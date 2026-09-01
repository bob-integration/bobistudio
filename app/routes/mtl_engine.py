# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Moteur ST 2110 (2110_io / MTL-DPDK) : flux composables RX/TX (« Option A »), budgets de
files AF-XDP / lcores, détection d'opération disruptive (relance moteur), épinglage de port,
et préparation de l'hôte local (IOMMU/hugepages/ice/vfio — cf. app/mtl.py).

Ce module était historiquement niché SANS EN-TÊTE dans la section « Câblage » (le flux
RX/TX composable) et dans une section « MTL » distincte (la préparation hôte) ; les deux
sont regroupés ici car elles forment un seul domaine (le moteur 2110_io) une fois remises
bout à bout. `_compute_receivers_detail`/`_io_media_ports` restent dans __init__.py (domaine
NMOS non encore extrait) — importés localement là où c'est nécessaire."""

import logging
from ..numerotation import slot_tx, slot_rx, flux_video, flux_audio, flux_anc
import threading

from flask import jsonify, request

from . import bp, _req_host
from .shared import _load_dc, _mtl_total_queues
from ..auth import require_perm, require_login
from ..database import db_add_alert
from ..deploy import deployer_script
from ..vmlocks import verrou_vmid

log = logging.getLogger(__name__)


# ─── Flux composables RX/TX (« Option A ») ───────────────────────────────────
def _tx_gate(vmid, op, args, data):
    """Garde-fou étage 2 (docs/reference/TX_LAYOUTS.md) commun aux actions TX éditables : CALCULE le verdict
    (`tx_maintenance.classify` — perturbatrice = elle fait apparaître une session TX sur un port en
    rate-limiter, donc un `rte_tm_hierarchy_commit` = stop/start du port ~1 s) et :
      · action SÛRE            → (None, verdict) : la route applique normalement ;
      · `defer:true`           → l'action part au BAC (fenêtre de maintenance) → réponse 202 ;
      · perturbatrice sans     → 409 `needs_confirm` + verdict NOMMANT les sorties qui vont figer ;
        `confirm:true`
      · perturbatrice confirmée→ (None, verdict) : on applique tout de suite.
    Retourne (response|None, verdict)."""
    from .. import tx_maintenance as _txm
    from ..auth import current_user
    try:
        verdict = _txm.classify(vmid, _txm.preview(vmid, op, args), op=op)
    except Exception as e:
        # Le classement ne doit jamais BLOQUER une action (le moteur reste pilotable même si le
        # calcul de verdict casse) — mais un garde-fou qui tombe en silence est pire que pas de
        # garde-fou : on le rend VISIBLE (alerte + log error) au lieu de laisser passer sans bruit.
        log.error("tx gate %s/%s: verdict incalculable (action appliquée sans classement) : %s",
                  vmid, op, e)
        db_add_alert("alert.docker.classement_impossible", "warning", vmid=vmid, kind="tx_stall",
                     params={"vmid": vmid, "op": op, "e": e})
        return None, {"level": "unknown", "error": str(e)}
    if bool(data.get("defer")):
        try:
            actor = (current_user() or {}).get("username")
        except Exception:
            actor = ""
        pid, err = _txm.queue(vmid, op, args, apply_at=(data.get("apply_at") or None), actor=actor)
        if err:
            return (jsonify({"ok": False, "error": err}), 400), verdict
        return (jsonify({"ok": True, "deferred": True, "pending_id": pid,
                         "verdict": verdict,
                         "pending": _txm.list_pending(vmid)}), 202), verdict
    if verdict.get("level") == "disruptive" and not bool(data.get("confirm")):
        return (jsonify({"ok": False, "needs_confirm": True, "verdict": verdict,
                         "reason": verdict.get("reason")}), 409), verdict
    return None, verdict


@bp.route("/api/mtl/<int:vmid>/tx/<int:slot>/dest", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_tx_dest(vmid, slot):
    """Règle À CHAUD la destination (mcast/port) d'un flux d'un slot TX d'un moteur MTL, par ESSENCE
    (video|audio|anc) et leg 2022-7 (0|1). Persiste dans deploy_config.tx_slots[slot] + pousse au
    contrôleur (:8081/tx). La vidéo (essence=video, leg=0) pilote l'émission ; audio/ANC + legs sont
    enregistrés (structure prête, émis quand l'essence sera implémentée). Notifie NMOS."""
    import json as _json
    from ..database import db_get_container, db_update_deploy_config
    from .. import docker_driver
    data = request.json or {}
    essence = (data.get("essence") or "video").lower()
    leg = 1 if int(data.get("leg") or 0) == 1 else 0
    if essence not in ("video", "audio", "anc"):
        return jsonify({"ok": False, "error": "essence invalide (video|audio|anc)"}), 400
    mcast = (data.get("mcast") or "").strip()
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not mcast or not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "mcast/port invalides"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": "container introuvable"}), 404
    try:
        dc = _json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    slots = list(params.get("tx_slots") or [])
    if not (0 <= slot < len(slots)):
        return jsonify({"ok": False, "error": f"slot TX #{slot} inexistant"}), 400
    # C2b+ : si ce flux est lié à une ressource NMOS (rebinding explicite), son transport est piloté
    # par la ressource (push-down) → on refuse l'édition directe du slot (éditer la ressource, ou délier).
    _nbind = params.get("nmos_bind") or {}
    try:
        _aidx = max(0, min(1, int(data.get("audio_idx") or 0)))
    except (TypeError, ValueError):
        _aidx = 0
    _sk = {"video": slot_tx(slot, "v"), "anc": slot_tx(slot, "d"), "audio": slot_tx(slot, "a%d" % _aidx)}[essence]
    if _nbind.get(_sk):
        return jsonify({"ok": False, "error": "flux lié à une ressource NMOS — éditez la ressource ou déliez le slot"}), 409
    # Règle de plage STRICTE éventuelle sur le port physique résolu pour ce slot/leg : un switch qui
    # contraint les adresses par port rejetterait physiquement une adresse hors plage.
    if c.get("node_id"):
        import ipaddress as _ipa
        from .. import allocations as _alloc
        _ifn, _netid = _alloc._egress_iface(c.get("node_id"), params, slot, leg=leg)
        _rule = _alloc._plage_applicable(c.get("node_id"), _ifn, media_network_id=_netid, essence=essence, leg=leg)
        if _rule is not None:
            try:
                _base = int(_ipa.IPv4Address(_rule["base_ip"]))
                _cand = int(_ipa.IPv4Address(mcast))
                _in_range = _base <= _cand < _base + int(_rule.get("size") or 0)
            except Exception:
                _in_range = False
            if not _in_range:
                return jsonify({"ok": False, "error":
                    f"adresse hors plage autorisée sur ce port ({_rule.get('label') or _rule['base_ip']}"
                    f"/+{_rule.get('size')}) — {_ifn or '?'}"}), 400
    # Ledger de réservation atomique (cf. app/allocations.py) : garde le "tx:{vmid}:" cohérent
    # avec l'allocation automatique pour que la libération à la destruction du container ramasse
    # aussi les overrides manuels. Libère d'abord une éventuelle réservation précédente de CE
    # même override (l'opérateur change d'avis) avant de réserver la nouvelle adresse.
    from ..database import db_reserve_mcast, db_release_mcast_owner
    _override_ref = f"tx:{vmid}:{slot}:{essence}:override:{_aidx}:leg{leg}"
    # Étage 2 : changer la destination change la SIGNATURE de session (mtl_rx.compute_sig) → ancienne
    # libérée + NOUVELLE créée = commit TM = stop/start du port en narrow. Le verdict est calculé AVANT
    # de toucher au ledger multicast : un refus (409, faute de confirmation) ne doit RIEN modifier —
    # relâcher la réservation après coup libérerait aussi l'adresse ACTUELLEMENT émise par ce flux
    # (le ref d'override est le même), qu'un autre flux pourrait alors s'attribuer.
    _gate, _verdict = _tx_gate(vmid, "tx_dest",
                               {"slot": slot, "essence": essence, "leg": leg,
                                "audio_idx": _aidx, "mcast": mcast, "port": port}, data)
    if _gate is not None and _gate[1] == 409:
        return _gate
    # Ledger de réservation atomique (cf. app/allocations.py) : garde le "tx:{vmid}:" cohérent
    # avec l'allocation automatique pour que la libération à la destruction du container ramasse
    # aussi les overrides manuels. Libère d'abord une éventuelle réservation précédente de CE
    # même override (l'opérateur change d'avis) avant de réserver la nouvelle adresse. Un changement
    # DIFFÉRÉ réserve dès maintenant : l'adresse ne doit pas être soufflée avant l'application.
    db_release_mcast_owner(_override_ref)
    if not db_reserve_mcast(mcast, port, _override_ref):
        return jsonify({"ok": False, "error": f"adresse {mcast}:{port} déjà utilisée par un autre flux"}), 409
    if _gate is not None:                          # différé (202) : réservé, mais pas encore appliqué
        return _gate
    slots[slot] = dict(slots[slot] or {})
    if essence == "audio":
        # Audio : liste de 2 entrées indexées par audio_idx (0 ou 1)
        audio_idx = _aidx
        audios = list(slots[slot].get("audios") or [{}, {}])
        while len(audios) <= audio_idx:
            audios.append({})
        audios[audio_idx] = dict(audios[audio_idx])
        sfx = "_leg1" if leg == 1 else ""
        audios[audio_idx]["multicast_ip" + sfx] = mcast
        audios[audio_idx]["dest_port" + sfx] = port
        slots[slot]["audios"] = audios
    else:
        # Vidéo + ANC : clés préfixées (video=pas de préfixe, anc=anc_) + suffixe leg1.
        pfx = "" if essence == "video" else essence + "_"
        sfx = "_leg1" if leg == 1 else ""
        slots[slot]["{}multicast_ip{}".format(pfx, sfx)] = mcast
        slots[slot]["{}dest_port{}".format(pfx, sfx)] = port
    params["tx_slots"] = slots
    db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)
    def _push_tx():
        with verrou_vmid(vmid, op="tx-push"):
            docker_driver.push_tx_slots(vmid, params)
    threading.Thread(target=_push_tx, daemon=True).start()
    try:
        from services import nmos
        nmos.notify_state_change()
    except Exception:
        pass
    return jsonify({"ok": True, "mcast": mcast, "port": port})


@bp.route("/api/mtl/<int:vmid>/tx/<int:slot>/format", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_tx_format(vmid, slot):
    """Règle le FORMAT du générateur (mire) d'un slot TX : width/height/fps/scan. Persiste dans
    deploy_config.tx_slots[slot] + pousse au contrôleur (push_tx_slots). Gouverne les slots en GÉN :
    sans câble, ou dont le générateur est explicitement activé (bouton GEN, qui prime sur le câble).
    Un slot câblé qui suit sa source ignore ce format (cf. push_tx_slots). Notifie NMOS (le SDP du
    sender change avec la résolution)."""
    import json as _json
    from ..database import db_get_container, db_update_deploy_config
    from .. import docker_driver
    data = request.json or {}
    try:
        w = int(data.get("width") or 0)
        h = int(data.get("height") or 0)
        fps = float(data.get("fps") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "width/height/fps invalides"}), 400
    scan = "i" if str(data.get("scan") or "p").lower() == "i" else "p"
    if not (16 <= w <= 8192 and 16 <= h <= 8192 and 1 <= fps <= 240):
        return jsonify({"ok": False, "error": "format hors bornes"}), 400
    # Étage 3 : « aligner la sortie sur sa source » pousse aussi l'ORDRE DE CHAMP et la PROFONDEUR —
    # les deux entrent dans la signature de session (mtl_rx.c:compute_sig) ; les omettre laisserait un
    # écart résiduel derrière un alignement présenté comme réussi (et donc un commit au câblage).
    fo = str(data.get("field_order") or "").lower()
    fo = fo if fo in ("tff", "bff") else ""
    try:
        bd = int(data.get("bit_depth") or 0)
    except (TypeError, ValueError):
        bd = 0
    if bd and bd not in (8, 10, 12, 16):
        return jsonify({"ok": False, "error": "profondeur invalide"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": "container introuvable"}), 404
    try:
        dc = _json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    slots = list(params.get("tx_slots") or [])
    if not (0 <= slot < len(slots)):
        return jsonify({"ok": False, "error": f"slot TX #{slot} inexistant"}), 400
    # C2b+ : slot lié à une ressource NMOS → format piloté par la ressource (push-down).
    if (params.get("nmos_bind") or {}).get(slot_tx(slot, "v")):
        return jsonify({"ok": False, "error": "slot lié à une ressource NMOS — éditez la ressource ou déliez le slot"}), 409
    # Étage 2 : le format entre dans la signature de session → une session est recréée (commit TM)
    # SAUF si le slot est câblé et suit sa source (le format poussé vaut 0 → aucun changement de sig).
    _gate, _verdict = _tx_gate(vmid, "tx_format",
                               {"slot": slot, "width": w, "height": h, "fps": fps, "scan": scan,
                                "field_order": fo, "bit_depth": bd}, data)
    if _gate is not None:
        return _gate
    slots[slot] = dict(slots[slot] or {})
    slots[slot]["width"], slots[slot]["height"] = w, h
    if bd:
        slots[slot]["bit_depth"] = bd
    # ST 2110-20 : en entrelacé on stocke la cadence TRAME (jamais la cadence CHAMP/field rate).
    # Une UI qui envoie la cadence champ (p.ex. 50 pour du 1080i50) émettrait un SDP
    # « exactframerate=50; interlace » non conforme → RX abonné en 50i, 0 trame. On ramène à la
    # cadence trame ; cohérent avec la normalisation idempotente du hook deploy (2110_io/hooks.py).
    if scan == "i" and fps > 30:
        fps = fps / 2.0
    slots[slot]["fps"], slots[slot]["scan"] = fps, scan
    if scan == "i":
        if fo:
            slots[slot]["field_order"] = fo
        else:
            slots[slot].setdefault("field_order", "tff")
    else:
        slots[slot]["field_order"] = ""
    params["tx_slots"] = slots
    db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)
    def _push_tx():
        with verrou_vmid(vmid, op="tx-push"):
            docker_driver.push_tx_slots(vmid, params)
    threading.Thread(target=_push_tx, daemon=True).start()
    try:
        from services import nmos
        nmos.notify_state_change()
    except Exception:
        pass
    return jsonify({"ok": True, "width": w, "height": h, "fps": fps, "scan": scan,
                    "field_order": slots[slot].get("field_order") or "",
                    "bit_depth": slots[slot].get("bit_depth") or 8})


@bp.route("/api/mtl/<int:vmid>/tx/<int:slot>/pacing", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_tx_pacing(vmid, slot):
    """Règle le RYTHME D'ÉMISSION d'un slot TX (mode tranche) : epoch_shift_us=0 → « attendre
    l'image suivante » (défaut, émission alignée sur l'epoch nominal) ; >0 → grille d'émission
    décalée de N µs après l'epoch (l'image part dès que les premières tranches sont prêtes ;
    TROFF déclaré dans le SDP, timestamp RTP inchangé → sync A/V préservée côté récepteur).
    Persiste dans deploy_config.tx_slots[slot] + pousse au contrôleur (push_tx_slots).
    Notifie NMOS (le SDP du sender gagne/perd son a=troff)."""
    import json as _json
    from ..database import db_get_container, db_update_deploy_config
    from .. import docker_driver
    data = request.json or {}
    try:
        shift = int(data.get("epoch_shift_us") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "epoch_shift_us invalide"}), 400
    # Borne haute : rester nettement sous une période trame (20 ms @50p) — au-delà on retomberait
    # dans l'epoch suivant et le décalage n'aurait plus de sens.
    if not (0 <= shift <= 15000):
        return jsonify({"ok": False, "error": "epoch_shift_us hors bornes (0–15000 µs)"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": "container introuvable"}), 404
    try:
        dc = _json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    slots = list(params.get("tx_slots") or [])
    if not (0 <= slot < len(slots)):
        return jsonify({"ok": False, "error": f"slot TX #{slot} inexistant"}), 400
    # Étage 2 : `epoch_shift_us` est DANS la signature de session mtl_rx → recréation = commit TM.
    _gate, _verdict = _tx_gate(vmid, "tx_pacing", {"slot": slot, "epoch_shift_us": shift}, data)
    if _gate is not None:
        return _gate
    slots[slot] = dict(slots[slot] or {})
    slots[slot]["epoch_shift_us"] = shift
    params["tx_slots"] = slots
    db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)
    def _push_tx():
        with verrou_vmid(vmid, op="tx-push"):
            docker_driver.push_tx_slots(vmid, params)
    threading.Thread(target=_push_tx, daemon=True).start()
    try:
        from services import nmos
        nmos.notify_state_change()
    except Exception:
        pass
    return jsonify({"ok": True, "epoch_shift_us": shift})


@bp.route("/api/mtl/<int:vmid>/tx/<int:slot>/serve_newest", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_tx_serve_newest(vmid, slot):
    """Règle le CHOIX DE LA TRAME ÉMISE d'un slot TX (mode tranche).

    1 → le moteur émet la trame la plus RÉCEMMENT prête et libère les périmées.
    0 (défaut, cf. `docker_driver.TX_SERVE_NEWEST_DEFAUT`) → comportement historique : il émet
      la plus ANCIENNE de son anneau.

    ⚠ LE DÉFAUT EST REPASSÉ À 0 LE 2026-08-19 : à 1, une sortie de production est sortie STRIÉE
    (Horace, TX #2) pendant que sa jumelle au même réglage restait propre — le résultat dépend de
    la phase entre la publication du producteur et la lecture du TX. Activer 1 sur un slot reste
    légitime et rentable, mais c'est une DEMANDE, à vérifier à l'image sur cette sortie-là.

    MESURÉ le 2026-08-12 : la source publie sa trame ~6 ms après le début de son créneau et le
    transport vient la chercher 16,4 ms après ce même début — elle est donc prête à temps pour
    l'émission suivante. En servant la plus ancienne, on émettait une trame périmée pendant
    qu'une plus fraîche attendait : l'âge du contenu au récepteur passe de 62,4 à 42,4 ms,
    exactement une image, sans perte de cadence.

    Persiste dans deploy_config.tx_slots[slot] + pousse au contrôleur (push_tx_slots).
    ⚠ Le champ est DANS la signature de session mtl_rx → le changer recrée la session (bref
    silence à l'antenne), d'où le passage par le même garde-fou que le rythme d'émission."""
    import json as _json
    from ..database import db_get_container, db_update_deploy_config
    from .. import docker_driver
    data = request.json or {}
    try:
        val = 1 if int(data.get("serve_newest") or 0) else 0
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "serve_newest invalide"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": "container introuvable"}), 404
    try:
        dc = _json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    slots = list(params.get("tx_slots") or [])
    if not (0 <= slot < len(slots)):
        return jsonify({"ok": False, "error": f"slot TX #{slot} inexistant"}), 400
    _gate, _verdict = _tx_gate(vmid, "tx_serve_newest",
                               {"slot": slot, "serve_newest": val}, data)
    if _gate is not None:
        return _gate
    slots[slot] = dict(slots[slot] or {})
    slots[slot]["serve_newest"] = val
    params["tx_slots"] = slots
    db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)

    def _push_tx():
        with verrou_vmid(vmid, op="tx-push"):
            docker_driver.push_tx_slots(vmid, params)
    threading.Thread(target=_push_tx, daemon=True).start()
    return jsonify({"ok": True, "serve_newest": val})


@bp.route("/api/mtl/<int:vmid>/tx/fallback", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_tx_fallback(vmid):
    """Règle le repli TX automatique (signal émis quand aucun slot n'est câblé).
    Persiste dans deploy_config.params.tx_fallback + pousse au contrôleur via push_tx_slots."""
    import json as _json
    from ..database import db_get_container, db_update_deploy_config
    from .. import docker_driver
    data = request.json or {}
    mode = (data.get("mode") or "black").lower()
    if mode not in ("none", "black", "bars"):
        return jsonify({"ok": False, "error": "mode invalide (none|black|bars)"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": "container introuvable"}), 404
    try:
        dc = _json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = dict(dc.get("params") or {})
    params["tx_fallback"] = mode
    db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)
    def _push_tx():
        with verrou_vmid(vmid, op="tx-push"):
            docker_driver.push_tx_slots(vmid, params)
    threading.Thread(target=_push_tx, daemon=True).start()
    return jsonify({"ok": True, "mode": mode})


def _mtl_lcore_sessions(node):
    """Budget de sessions vidéo simultanées borné par les lcores du nœud. Le facteur limitant
    d'un scheduler libmtl (1 lcore) est le DÉBIT de paquets à traiter, pas le nombre de sessions
    → le budget par lcore dérive du quota Mb/s (`mtl_sch_quota_mbs` — la MÊME manette que le
    remplissage réel des schedulers et que le dimensionnement `_auto_lcores`) rapporté au débit
    d'une session vidéo au format du site. Au-delà, libmtl sature un sch → epoch drops → trames
    perdues + « RTP alignment failure » au récepteur."""
    from .. import settings as _st
    from ..docker_driver import _est_video_mbs
    try:
        quota = max(500, int(_st.get("mtl_sch_quota_mbs") or 2500))
        per_lcore = max(1, int(quota // _est_video_mbs({})))
    except Exception:
        per_lcore = 2
    raw = ((node or {}).get("lcores") or "").strip().lower()
    if raw in ("", "auto"):
        # lcores AUTO-dimensionnés au déploiement (docker_driver._auto_lcores) : un cpuset littéral
        # n'existe pas encore. Le budget = plafond de schedulers réservables = mtl_lcore_max −
        # (1 manager + 1 marge). Sans ce cas, parse_cpuset('auto')=[] → 1 lcore → cap quasi nul.
        try:
            lcore_max = int(_st.get("mtl_lcore_max") or 16)
        except Exception:
            lcore_max = 16
        n_lcores = max(1, lcore_max - 2)
    else:
        try:
            from ..core_pool import parse_cpuset
            n_lcores = len(parse_cpuset(raw)) or 3
        except Exception:
            n_lcores = 3
    return max(1, n_lcores) * max(1, per_lcore)

def _mtl_per_source_sessions(params, role="rx"):
    """Nombre de SESSIONS (= files AF-XDP, 1 par session libmtl) consommées par UNE source
    vidéo, RÔLE rx ou tx : 1 vidéo + N audio + M ANC. **Variable** — une source peut avoir 0, 1,
    2… flux audio (audio_per_video) et 0 ou 1 ANC. AVANT on supposait 3 fixe (1+1+1) ; faux dans
    les deux sens (vidéo-seule = 1 ; 1 vidéo+2 audio+1 ANC = 4). Aligné sur le contrôleur 0.22.16
    qui dimensionne les files au nb réel de sessions."""
    # « Option A » : compté depuis les flux ACTIFS réels (rx_flows/tx_flows). 1 session = 1 flux
    # (vidéo + audio + ANC). On renvoie un coût PAR vidéo = ceil(total / nb vidéos) → borne SUPÉRIEURE
    # (les appelants multiplient par active_*_count : ceil(total/n)×n ≥ total → jamais sous-alloué).
    # Identique à l'ancien 1+aper+anc_per pour une compo homogène.
    import math
    from .. import io2110_flows as _iof
    flows = _iof.active_flows(params, role)
    n_video = max(1, len([f for f in flows if f["essence"] == "video"]))
    total   = max(1, len(flows))
    return max(1, math.ceil(total / n_video))

def _mtl_media_port_count(node, params=None):
    """Nombre de ports média UTILES d'un nœud. Chaque port a son PROPRE budget de files AF-XDP
    (E810 : ~96/port) → le budget TOTAL du moteur = budget par port × nb de ports (le moteur répartit
    les sessions en round-robin sur les ports). ≥1 (mono-port).
    **SMPTE 2022-7** (params.smpte_2022_7) : une session dual-leg consomme ses files sur LES DEUX
    ports d'une paire → une paire complète ne compte que pour UN port de capacité utile."""
    try:
        from .. import docker_driver as _dd
        ports = _dd._media_ifaces(node) or []
        n = len(ports)
        if params and params.get("smpte_2022_7"):
            n -= len(_dd.media_port_pairs(node))
        return max(1, n)
    except Exception:
        return 1


def _mtl_active_caps(params, total_queues, node=None, tx_budget=None):
    """Plafonds (rx, tx) de slots vidéo ACTIVABLES, bornés par le budget de files AF-XDP partagé
    RX+TX ET par le budget de lcores. Chaque slot vidéo consomme `_mtl_per_source_sessions` files
    (1 vidéo + N audio + M ANC, **variable** — plus le ×3 forfaitaire). Le moteur réserve ~1 file
    TX minimale → on garde 1 de marge. Bornés aussi par la capacité provisionnée (video_count /
    tx_count). Reproduit l'ancien comportement quand chaque source = 1 vidéo+1 audio+1 ANC (3).

    ``tx_budget`` (socle DPDK narrow, cf. _mtl_rl_tx_budget) : budget TOTAL de sessions TX en
    pacing RL (feuilles du rate-limiter matériel — la limite dure de la carte, docs/chantiers/DPDK_NARROW.md §7).
    Sous DPDK les budgets RX et TX sont INDÉPENDANTS (files RSS RX vs feuilles RL TX) : le TX se
    borne sur son budget propre, le RX cesse de se voir amputer les files consommées par le TX."""
    active_rx = int(params.get("active_rx_count") or 0)
    active_tx = int(params.get("active_tx_count") or 0)
    prov_rx = int(params.get("video_count") or 0)
    prov_tx = int(params.get("tx_count") or 0)
    per_rx = _mtl_per_source_sessions(params, "rx")     # files/source RX (≥1)
    per_tx = _mtl_per_source_sessions(params, "tx")     # files/slot TX  (≥1)
    # Budget de FILES partagé RX+TX (1 file/session) ; ~1 file TX réservée d'office par le moteur.
    q_budget = max(1, int(total_queues) - 1)
    if tx_budget is not None:
        # DPDK/RL : budgets découplés — RX sur les files RSS (total_queues), TX sur les feuilles RL.
        cap_rx = min(prov_rx, max(0, q_budget // per_rx))
        cap_tx = min(prov_tx, max(0, int(tx_budget) // per_tx))
    else:
        used_rx_q = active_rx * per_rx
        used_tx_q = active_tx * per_tx
        cap_rx = min(prov_rx, max(0, (q_budget - used_tx_q) // per_rx))
        cap_tx = min(prov_tx, max(0, (q_budget - used_rx_q) // per_tx))
    # Garde-fou lcore (en sessions vidéo : budget par scheduler dérivé du quota Mb/s).
    lc = _mtl_lcore_sessions(node)
    cap_rx = min(cap_rx, max(0, lc - active_tx))
    cap_tx = min(cap_tx, max(0, lc - active_rx))
    return cap_rx, cap_tx


def _mtl_rl_tx_budget(rx_blk, params, node):
    """Budget TOTAL de sessions TX sous pacing RL (socle DPDK narrow) : cap RL PAR PORT × nb de
    ports média utiles (pair-aware 2022-7 via _mtl_media_port_count — le leg redondant consomme
    sa feuille sur l'autre port de la paire). Cap par port : valeur LIVE du moteur
    (rl_tx_cap_per_port, :8080 bloc rl) si disponible, sinon dérivée orchestrateur (nœud à PF
    dpdk + pacing rl → profil mesuré nic_profiles / bibliothèque de cartes, cf. _node_rl_tx_cap).
    None si le nœud n'est pas en RL/dpdk → les appelants gardent le budget AF-XDP partagé."""
    cap_pp = (rx_blk or {}).get("rl_tx_cap_per_port")
    if not cap_pp:
        try:
            from .. import docker_driver as _dd
            if node and _dd._has_dpdk_pf(node):
                _pc, _ = _dd._derive_pacing(node)
                # _derive_pacing → None si aucun profil posé ; le contrôleur retombe alors sur
                # MTL_PACING=auto = RL sur port E810 dpdk → traiter None comme 'rl'.
                if (_pc or "rl") == "rl":
                    cap_pp = _dd._node_rl_tx_cap(node)
        except Exception:
            cap_pp = None
    if not cap_pp:
        return None
    return int(cap_pp) * _mtl_media_port_count(node, params)

def _mtl_apply_flow_change(vmid, params, role, notify=True, push=True, recreate=False):
    """Re-normalise (alloc mcast/port/shm des nouveaux flux via le hook before_deploy), persiste le
    deploy_config, puis applique : push TX (role tx) + rebuild NMOS. NB : ``notify_state_change`` ne
    fait que reconstruire le modèle NMOS en mémoire (+ re-register externe) — il ne touche JAMAIS
    l'agent du conteneur. Le teardown réel d'une session RX retirée est poussé EXPLICITEMENT par
    ``_mtl_teardown_rx_flows`` (unsubscribe :8081) AVANT cet appel ; le moteur la libère à chaud.

    ``recreate`` (décidé par la route via ``_mtl_op_is_disruptive`` + confirmation utilisateur) → on
    RECRÉE le conteneur (re-réserve les files au boot, re-câble RX/TX). Sinon, chemin À CHAUD (push TX,
    ou rien pour RX que le moteur réconcilie). Les ENV `ACTIVE_*_COUNT`/réserve étant figés au `docker
    run`, seule la recréation prend en compte une demande au-delà du budget bootté."""
    from ..database import db_update_deploy_config, db_get_container
    from .. import plugins as _plg, settings as _st
    _hook = _plg.get_hook("2110_io", "before_deploy")
    if _hook:
        try:
            _c = db_get_container(vmid) or {}
            params = _hook(params, {"vmid": vmid, "node_id": _c.get("node_id"), "settings": _st.all()})
        except Exception as _e:
            log.warning("flow change %d: before_deploy: %s", vmid, _e)
    db_update_deploy_config(vmid, "2110_io", params)
    try:
        from .. import docker_driver
        if recreate:
            # Op disruptive CONFIRMÉE (au-delà de la réserve figée / budget bootté) → recréation.
            db_add_alert("alert.docker.recreation_budget", "warning", vmid=vmid, kind="tx_stall",
                         params={"vmid": vmid})
            docker_driver.deploy_docker(vmid, params, type_script="2110_io")
        elif push and role == "tx":
            docker_driver.push_tx_slots(vmid, params)
        if notify:
            from services import nmos as _nmos
            _nmos.notify_state_change()
    except Exception as _e:
        log.warning("flow change %d: apply: %s", vmid, _e)
    return params


def _engine_booted_env_int(vmid, envkey):
    """Valeur entière d'une variable d'ENV FIGÉE AU BOOT du conteneur moteur 2110_io, lue via
    `docker inspect`. None si indéterminable (conteneur absent / SSH KO). Les budgets `ACTIVE_RX_COUNT`/
    `ACTIVE_TX_COUNT` ne sont relus qu'au `docker run` → c'est la source de vérité pour décider d'une
    recréation quand la demande dépasse le budget bootté."""
    import shlex as _shlex
    try:
        from ..database import db_get_container, db_get_node
        from ..docker_driver import ssh_run, _name
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id"))
        if not node:
            return None
        name = c.get("docker_name") or _name(vmid)
        rc, out, _ = ssh_run(
            node["host"],
            "docker inspect %s --format '{{range .Config.Env}}{{println .}}{{end}}'" % _shlex.quote(name),
            timeout=10)
        if rc != 0:
            return None
        pfx = envkey + "="
        for line in (out or "").splitlines():
            if line.startswith(pfx):
                return int(line.split("=", 1)[1] or 0)
    except Exception:
        return None
    return None

@bp.route("/api/mtl/<int:vmid>/engine_logs", methods=["GET"])
@require_perm("containers.deploy")
def mtl_engine_logs(vmid):
    """Journal du conteneur moteur 2110_io — VUE SPÉCIALISÉE de la route générique
    `GET /api/containers/<vmid>/logs` (cf. `app/routes/container_logs.py`).

    Le corps (lecture `journalctl` sur l'hôte, filtres `since`/`until`/`priority`/`grep`, plafond
    dur sur `lines`, rétention) est MUTUALISÉ : cette route n'ajoute que les deux champs propres au
    moteur — `level` = niveau libmtl FIGÉ AU BOOT du conteneur (`docker inspect`), `setting` = le
    réglage COURANT ; les deux diffèrent tant que le moteur n'a pas été redéployé, et c'est
    précisément ce qu'on veut voir. Même permission (`containers.deploy` et non le simple login :
    ces journaux portent la topologie du site — IP média, multicast, SDP).

    Différence de fond avec la version précédente : la source n'est plus `docker logs` (qui meurt
    avec le conteneur, alors que le moteur tourne en `--rm` et est recréé à chaque redéploiement)
    mais le journal de l'HÔTE → l'historique d'un moteur détruit reste consultable."""
    from .container_logs import _logs_payload
    from .. import settings as _st
    data, status = _logs_payload(vmid)
    data["level"] = _engine_booted_log_level(vmid)
    data["setting"] = str(_st.get("mtl_log_level") or "warning").strip().lower()
    return jsonify(data), status


def _engine_booted_log_level(vmid):
    """Niveau de log libmtl FIGÉ AU BOOT du conteneur (env `MTL_LOG_LEVEL` via `docker inspect`).
    Diffère du réglage courant tant que le moteur n'a pas été redéployé. None si indéterminable."""
    import shlex as _shlex
    try:
        from ..database import db_get_container, db_get_node
        from ..docker_driver import ssh_run, _name
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id"))
        if not node:
            return None
        name = c.get("docker_name") or _name(vmid)
        rc, out, _ = ssh_run(
            node["host"],
            "docker inspect %s --format '{{range .Config.Env}}{{println .}}{{end}}'" % _shlex.quote(name),
            timeout=10)
        if rc != 0:
            return None
        for line in (out or "").splitlines():
            if line.startswith("MTL_LOG_LEVEL="):
                return (line.split("=", 1)[1] or "").strip() or None
    except Exception:
        return None
    return None


def _engine_booted_active_tx(vmid):
    """ACTIVE_TX_COUNT bootté du moteur (env figé). None si indéterminable → repli push à chaud."""
    return _engine_booted_env_int(vmid, "ACTIVE_TX_COUNT")

def _engine_booted_active_rx(vmid):
    """ACTIVE_RX_COUNT bootté du moteur (env figé). None si indéterminable."""
    return _engine_booted_env_int(vmid, "ACTIVE_RX_COUNT")

def _mtl_op_is_disruptive(vmid, params_after, role=None, op="add"):
    """Une opération moteur 2110_io est DISRUPTIVE si elle va relancer `mtl_init` / recréer le conteneur
    → coupure brève de TOUS les flux (RX, TX et consommateurs aval). On ne demande confirmation QUE
    dans ce cas ; les ops à chaud (sous la réserve figée) passent sans gêne. Retourne (bool, raison).

    Signal de vérité = le MÊME que la barre XDP de l'UI : la DEMANDE par port (flux provisionnés,
    assignation modulo/épinglage) dépasse-t-elle la RÉSERVE de files figée au dernier `mtl_init`
    (`nic_ports[].xdp_reserved`, live). Plus, côté TX, le budget bootté `ACTIVE_TX_COUNT`."""
    if op in ("deploy", "restart", "realign"):
        return True, "redéploiement/redémarrage du moteur — coupure brève de TOUS les flux"
    if role == "tx":
        _booted = _engine_booted_active_tx(vmid)
        _desired = int((params_after or {}).get("active_tx_count") or 0)
        if _booted is not None and _desired > _booted:
            return True, ("sortie TX au-delà du budget bootté (ACTIVE_TX_COUNT=%d) — recréation du "
                          "moteur, coupure brève de tous les flux" % _booted)
    try:
        from ..database import db_get_node, db_get_container
        from .. import io2110_flows as _iof
        from .nmos_detail import _compute_receivers_detail, _io_media_ports
        blk = next((b for b in _compute_receivers_detail(only_vmid=vmid)), {}) or {}
        nic_ports = blk.get("nic_ports") or []
        node = db_get_node((db_get_container(vmid) or {}).get("node_id"))
        if nic_ports:
            reserved_by = {p["iface"]: (p.get("xdp_reserved") or 0)
                           for p in nic_ports if p.get("xdp_reserved")}
            _, _, _, _slot_rx = _io_media_ports(node, params_after, "rx_pins")
            _, _, _, _slot_tx = _io_media_ports(node, params_after, "tx_pins")
            planned = {}
            for _f in _iof.active_flows(params_after, "rx"):
                _p = (_slot_rx(int(_f.get("idx") or 0)) or {}).get("iface")
                if _p: planned[_p] = planned.get(_p, 0) + 1
            for _f in _iof.active_flows(params_after, "tx"):
                _p = (_slot_tx(int(_f.get("idx") or 0)) or {}).get("iface")
                if _p: planned[_p] = planned.get(_p, 0) + 1
            for _iface, _pl in planned.items():
                if _iface in reserved_by and _pl > reserved_by[_iface]:
                    return True, ("le port %s dépasserait sa réserve de files (%d > %d) — redéploiement "
                                  "du moteur, coupure brève de tous les flux" % (_iface, _pl, reserved_by[_iface]))
        elif blk.get("xdp_reserved"):
            _planned = len(_iof.active_flows(params_after, "rx")) + len(_iof.active_flows(params_after, "tx"))
            _reserved = blk.get("xdp_reserved")
            if _planned > _reserved:
                return True, ("dépassement de la réserve de files (%d > %d) — redéploiement du moteur, "
                              "coupure brève de tous les flux" % (_planned, _reserved))
        else:
            # Réserve par-port indéterminable (moteur injoignable / état partiel) → repli sur le budget
            # bootté (env figé), même axe que le TX : active_rx_count vs ACTIVE_RX_COUNT. Au-delà, une
            # recréation est requise. (Le dépassement de réserve par AJOUT de flux est, lui, couvert par
            # la branche per-port quand l'état live est disponible — cas normal.)
            _desired_rx = int((params_after or {}).get("active_rx_count") or 0)
            _booted_rx = _engine_booted_active_rx(vmid)
            if _booted_rx is not None and _desired_rx > _booted_rx:
                return True, ("budget RX au-delà du bootté (ACTIVE_RX_COUNT=%d) — recréation du moteur, "
                              "coupure brève de tous les flux" % _booted_rx)
    except Exception as _e:
        log.warning("disruptive check vmid=%s: %s", vmid, _e)
    return False, ""


def _mtl_flow_add(params, role, essence, attached_to=None, label=""):
    """Ajoute un flux à rx_flows/tx_flows (idx libre du pool). Retourne (flow, None) ou (None, err)
    si le pool de cette essence est saturé / l'attache invalide."""
    from .. import io2110_flows as _iof
    key = "rx_flows" if role == "rx" else "tx_flows"
    flows = _iof.normalize(params.get(key) or _iof.active_flows(params, role))
    poolmap = ({"video": "video_count", "audio": "audio_count", "anc": "anc_count"} if role == "rx"
               else {"video": "tx_count", "audio": "tx_audio_count", "anc": "anc_count"})
    cap = int(params.get(poolmap[essence]) or 0)
    idx = _iof.free_idx(flows, essence)
    if cap and idx >= cap:
        return None, (f"Capacité du pool {essence} atteinte ({cap}) — augmentez le pool "
                      "puis redéployez le moteur.")
    if essence == "video":
        attached_to = None
    elif attached_to and not any(f["essence"] == "video" and f["id"] == attached_to for f in flows):
        return None, "Vidéo d'attache introuvable."
    flow = {"id": _iof._new_id(), "essence": essence, "idx": idx,
            "attached_to": attached_to, "label": label or ""}
    flows.append(flow)
    params[key] = flows
    return flow, None


def _mtl_teardown_rx_flows(vmid, removed):
    """D-facile : libère À CHAUD les sessions RX retirées. Pour chaque flux RX réellement abonné, on
    pousse l'unsubscribe (``enable=False``) au contrôleur (:8081/nmos/subscribe) → le daemon mtl_rx
    retire la session de son ensemble désiré et ``reconcile()`` la libère (free-before-create,
    ``mt_rx_xdp_put`` rend la file AF-XDP au pool) SANS ``mtl_uninit`` ni faute PTP. Remplace la
    « libération différée au prochain redéploiement » : le moteur sait déjà le faire à chaud, seul
    l'orchestrateur ne poussait pas le teardown. Un slot en GÉN/simu (non abonné) n'a rien à libérer.
    Retourne le nombre de sessions effectivement coupées."""
    from services import nmos as _nmos
    n = 0
    for f in removed or []:
        idx = f.get("idx")
        ess = f.get("essence", "video")
        if idx is None:
            continue
        try:
            if not _nmos.active_sdp_for(vmid, idx, ess):
                continue   # slot non abonné (GÉN/simu) → aucune session moteur à couper
            _nmos.manual_subscribe(vmid, idx, ess, None, enable=False)
            n += 1
        except Exception as e:
            log.warning("mtl teardown rx vmid=%s idx=%s/%s: %s", vmid, idx, ess, e)
    return n


@bp.route("/api/mtl/<int:vmid>/activate", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_activate(vmid):
    """Ajoute une Source (RX) / Destination (TX) COMPLÈTE à un moteur 2110_io : 1 vidéo + ses
    audios/ANC par défaut (mémoire de la compo legacy). Body : {kind:"rx"|"tx"}. Édite rx_flows/
    tx_flows (« Option A ») + applique à chaud. Le bouton « + » historique reste fonctionnel ;
    le contrôle granulaire (un audio/ANC) passe par /flows/add."""
    from ..database import db_get_container, db_get_node
    from .. import io2110_flows as _iof
    from .nmos_detail import _compute_receivers_detail
    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    if kind not in ("rx", "tx"):
        return jsonify({"ok": False, "error": "kind doit être 'rx' ou 'tx'"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dict(dc.get("params") or {})
    # Garde-fou budget NIC/CPU (queues XDP / lcores) — comme avant l'« Option A ». Le budget de files
    # est AGRÉGÉ sur tous les ports média (× nb de ports) : chaque port a son propre budget AF-XDP. On
    # prend le plafond HW LIVE du moteur (sinon réglage) pour rester cohérent avec le bouton « + » (api_io_mtl).
    _node_cap = db_get_node(c.get("node_id"))
    _rx_blk_cap = next((_b for _b in _compute_receivers_detail(only_vmid=vmid)), {}) or {}
    _live_hw = _rx_blk_cap.get("xdp_hw_per_port")
    cap_rx, cap_tx = _mtl_active_caps(
        params,
        _mtl_total_queues({"hw_max_combined": _live_hw} if _live_hw else None) * _mtl_media_port_count(_node_cap, params),
        node=_node_cap,
        # Socle DPDK narrow : le TX se borne sur le budget RL (cap/port × ports), pas sur les
        # files AF-XDP — même plafond que le bouton « + Ajouter un TX » (Destinations).
        tx_budget=_mtl_rl_tx_budget(_rx_blk_cap, params, _node_cap))
    flows = _iof.normalize(params.get("rx_flows" if kind == "rx" else "tx_flows")
                           or _iof.active_flows(params, kind))
    cur_vid = len([f for f in flows if f["essence"] == "video"])
    if (kind == "rx" and cur_vid >= cap_rx) or (kind == "tx" and cur_vid >= cap_tx):
        return jsonify({"ok": False, "error": "Budget NIC/CPU atteint (queues XDP ou lcores) — "
                        "désactivez une source/destination ou augmentez les queues/lcores du nœud, "
                        "ou redéployez avec un pool plus grand."}), 400
    # Compo par défaut d'une source/destination : 1 vidéo + N audio + 1 ANC (mémoire du ratio legacy).
    ntot = int(params.get("video_count" if kind == "rx" else "tx_count") or 0)
    aper = max(1, (int(params.get("audio_count") or 0) // ntot) if ntot else 1)
    vid, err = _mtl_flow_add(params, kind, "video")
    if err:
        return jsonify({"ok": False, "error": err}), 400
    for _ in range(aper):
        _mtl_flow_add(params, kind, "audio", attached_to=vid["id"])
    if int(params.get("anc_count") or 0) > 0:
        _mtl_flow_add(params, kind, "anc", attached_to=vid["id"])
    # Pré-confirmation : ajout d'une source/destination au-delà de la réserve figée → recréation du
    # moteur (coupure de TOUS les flux). confirm:true requis, sinon 409 sans rien appliquer.
    _disruptive, _reason = _mtl_op_is_disruptive(vmid, params, kind)
    if _disruptive and not bool(data.get("confirm")):
        # Étage 2 : la modal doit NOMMER les sorties qui vont figer (verdict calculé, cf.
        # tx_maintenance.classify) — un ajout hors budget bootté recrée le conteneur (op='recreate').
        from .. import tx_maintenance as _txm
        return jsonify({"ok": False, "needs_confirm": True, "reason": _reason,
                        "verdict": dict(_txm.classify(vmid, params, op="recreate"),
                                        detail=_reason)}), 409
    params = _mtl_apply_flow_change(vmid, params, kind, recreate=_disruptive)
    return jsonify({"ok": True, "rx_flows": params.get("rx_flows"), "tx_flows": params.get("tx_flows"),
                    "active_rx_count": int(params.get("active_rx_count") or 0),
                    "active_tx_count": int(params.get("active_tx_count") or 0),
                    "video_count": int(params.get("video_count") or 0),
                    "tx_count": int(params.get("tx_count") or 0)})


@bp.route("/api/mtl/<int:vmid>/deactivate", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_deactivate(vmid):
    """Retire la DERNIÈRE Source (RX) / Destination (TX) complète (vidéo + ses audios/ANC attachés),
    symétrique de /activate. Body : {kind:"rx"|"tx"}. RX : teardown À CHAUD des flux retirés
    (_mtl_teardown_rx_flows) → le moteur libère la session + sa file XDP via reconcile(), sans réinit
    ni faute PTP. TX : push_tx_slots coupe le slot."""
    from ..database import db_get_container
    from .. import io2110_flows as _iof
    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    if kind not in ("rx", "tx"):
        return jsonify({"ok": False, "error": "kind doit être 'rx' ou 'tx'"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dict(dc.get("params") or {})
    key = "rx_flows" if kind == "rx" else "tx_flows"
    flows = _iof.normalize(params.get(key) or _iof.active_flows(params, kind))
    videos = [f for f in flows if f["essence"] == "video"]
    if not videos:
        return jsonify({"ok": False, "error": "aucune source/destination à retirer"}), 400
    last = videos[-1]
    removed = [f for f in flows if f["id"] == last["id"] or f.get("attached_to") == last["id"]]
    params[key] = [f for f in flows if f["id"] != last["id"] and f.get("attached_to") != last["id"]]
    # RX : teardown à chaud des sessions retirées AVANT le rebuild (le moteur libère la file XDP).
    if kind == "rx":
        _mtl_teardown_rx_flows(vmid, removed)
    params = _mtl_apply_flow_change(vmid, params, kind, notify=True)
    return jsonify({"ok": True, "rx_flows": params.get("rx_flows"), "tx_flows": params.get("tx_flows"),
                    "active_rx_count": int(params.get("active_rx_count") or 0),
                    "active_tx_count": int(params.get("active_tx_count") or 0),
                    "video_count": int(params.get("video_count") or 0),
                    "tx_count": int(params.get("tx_count") or 0),
                    "note": ("retiré ; file moteur libérée à chaud" if kind == "rx" else "retiré")})


@bp.route("/api/mtl/<int:vmid>/pin", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_pin(vmid):
    """Épingle (ou libère) le PORT d'un slot RX/TX d'un moteur 2110_io multi-NIC. Body :
    {role:"rx"|"tx", idx, iface|null}. iface vide/null → retour à la RÉPARTITION AUTO. Persiste
    params.rx_pins/tx_pins (survit au redéploiement) puis applique À CHAUD : RX via :8081/pin
    (le daemon reconcile déplace la session sur la nouvelle NIC, sans faute PTP), TX via
    push_tx_slots (le payload /tx porte désormais l'iface)."""
    from ..database import db_get_container, db_get_node, db_update_deploy_config
    from .. import docker_driver as _dd
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role")
    if role not in ("rx", "tx"):
        return jsonify({"ok": False, "error": "role doit être 'rx' ou 'tx'"}), 400
    # `idxs` (liste) : épingle D'UN COUP tous les flux d'un même ensemble. L'exploitant raisonne
    # par SOURCE ou par SORTIE, pas par essence — et une vidéo reçue sur une carte pendant que son
    # audio arrive sur l'autre n'est pas un réglage, c'est un accident. Le choix se fait donc au
    # niveau de l'ensemble, et cette route le pose sur chacun de ses flux en UNE opération : une
    # boucle de N requêtes côté navigateur laisserait un ensemble à moitié épinglé si l'une échoue.
    # `idx` seul reste accepté (appelants existants).
    try:
        if isinstance(data.get("idxs"), list):
            idxs = [int(x) for x in data["idxs"]]
            if not idxs:
                return jsonify({"ok": False, "error": "idxs vide"}), 400
        else:
            idxs = [int(data.get("idx"))]
    except Exception:
        return jsonify({"ok": False, "error": "idx invalide"}), 400
    idx = idxs[0]
    iface = (data.get("iface") or "").strip()   # "" → auto
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    node = db_get_node(c.get("node_id"))
    ports = [e["ifname"] for e in _dd._media_ifaces(node)] if node else []
    if iface and iface not in ports:
        return jsonify({"ok": False, "error": f"port inconnu: {iface}"}), 400
    params = dict(dc.get("params") or {})
    key = "rx_pins" if role == "rx" else "tx_pins"
    pins = dict(params.get(key) or {})
    for _i in idxs:
        if iface:
            pins[str(_i)] = iface
        else:
            pins.pop(str(_i), None)
    params[key] = pins
    db_update_deploy_config(vmid, "2110_io", params)
    # Application à chaud
    msg = ""
    try:
        if role == "tx":
            _dd.push_tx_slots(vmid, params)
        else:
            from ..addressing import get_container_ip
            from .. import deploy
            ip = get_container_ip(vmid)
            if ip:
                # Un appel par flux : l'agent déplace UNE session à la fois (le daemon reconcile
                # la repose sur la nouvelle NIC sans faute PTP). La persistance, elle, a déjà été
                # écrite en bloc plus haut — un échec ici ne laisse donc pas l'ensemble incohérent
                # au prochain déploiement.
                for _i in idxs:
                    deploy.agent_session().post(deploy.agent_url(ip, "/pin"),
                             json={"role": "rx", "idx": _i, "iface": iface or None}, timeout=5,
                             headers=deploy.agent_headers(vmid))
            else:
                msg = " (hôte injoignable — appliqué au prochain déploiement)"
    except Exception as e:
        log.warning("mtl pin vmid=%s %s/%s: %s", vmid, role, idx, e)
        msg = f" (push à chaud échoué : {e})"
    return jsonify({"ok": True, "role": role, "idx": idx, "iface": iface or None,
                    "pins": pins, "note": ("épinglé" if iface else "répartition auto") + msg})


@bp.route("/api/mtl/<int:vmid>/realign", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_realign(vmid):
    """Redéploie le moteur 2110_io avec ses params COURANTS (inchangés) → `_launch_mtl` repart propre
    (`_xdp_off` + `_flush_ntuple` + `mtl_init`) et réaligne les files XSK avec les règles flow-director.
    Remède d'une famine (slot RX/TX abonné/activé mais 0 fps). DISRUPTIF (coupe tous les flux) →
    confirm:true requis (le bouton « Redéployer pour réaligner » l'envoie via la modale)."""
    from ..database import db_get_container
    data = request.get_json(silent=True) or {}
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    if not bool(data.get("confirm")):
        from .. import tx_maintenance as _txm
        _rv = _txm.classify(vmid, (dc.get("params") or {}), op="realign")
        return jsonify({"ok": False, "needs_confirm": True, "verdict": _rv,
                        "reason": "Redéploiement du moteur pour réaligner les files — coupure brève de "
                                  "TOUS les flux (RX, TX et consommateurs aval)."}), 409
    _rp = dc.get("params") or {}
    def _realign():
        with verrou_vmid(vmid, op="realign"):
            deployer_script(vmid=vmid, type_script="2110_io", params=_rp,
                            script_path="/opt/script/main.py")
    threading.Thread(target=_realign).start()
    return jsonify({"ok": True, "status": "realign_en_cours", "vmid": vmid})


@bp.route("/api/mtl/<int:vmid>/flows/add", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_flow_add(vmid):
    """Ajoute UN flux composable (« Option A »), à chaud dans la limite du pool pré-provisionné.
    Body : {role:"rx"|"tx", essence:"video"|"audio"|"anc", attached_to?:<id|null>, label?}.
    - essence=video → nouvelle Source/Destination (groupe).
    - audio/anc + attached_to → rattaché à une vidéo (câblé/déplacé avec elle).
    - audio/anc sans attached_to → flux INDÉPENDANT."""
    from ..database import db_get_container
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role"); essence = data.get("essence")
    if role not in ("rx", "tx") or essence not in ("video", "audio", "anc"):
        return jsonify({"ok": False, "error": "role/essence invalide"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dict(dc.get("params") or {})
    flow, err = _mtl_flow_add(params, role, essence,
                              attached_to=data.get("attached_to") or None,
                              label=data.get("label") or "")
    if err:
        return jsonify({"ok": False, "error": err}), 400
    # Pré-confirmation : si l'ajout dépasse la réserve figée → relance/recréation du moteur (coupure
    # de TOUS les flux). On exige confirm:true ; sinon 409 sans rien appliquer (rien n'est persisté).
    _disruptive, _reason = _mtl_op_is_disruptive(vmid, params, role)
    if _disruptive and not bool(data.get("confirm")):
        from .. import tx_maintenance as _txm
        return jsonify({"ok": False, "needs_confirm": True, "reason": _reason,
                        "verdict": dict(_txm.classify(vmid, params, op="recreate"),
                                        detail=_reason)}), 409
    params = _mtl_apply_flow_change(vmid, params, role, recreate=_disruptive)
    return jsonify({"ok": True, "flow": flow,
                    "rx_flows": params.get("rx_flows"), "tx_flows": params.get("tx_flows")})


@bp.route("/api/mtl/<int:vmid>/flows", methods=["GET"])
@require_login
def api_mtl_flows(vmid):
    """Flux ACTIFS d'un moteur 2110 (RX et TX), avec leur niveau d'attention EFFECTIF.

    `alarmes` est renvoyé RÉSOLU (défauts appliqués, héritage audio/ANC→vidéo fait) : l'UI affiche
    ce qui vaut réellement, pas ce qui est persisté. Un écran qui montrerait une case vide là où le
    défaut alerte serait un mensonge sur l'état de surveillance."""
    from ..database import db_get_container
    from .. import io2110_flows as _iof
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dc.get("params") or {}
    hn = c.get("hostname") or f"vmid{vmid}"
    out = []
    for role in ("rx", "tx"):
        table = _iof.alarmes_par_slot(params, role)
        for f in _iof.active_flows(params, role):
            ess, idx = f.get("essence"), int(f.get("idx") or 0)
            # Nom de shm : uniquement en RX, où le moteur PRODUIT le flux. Un slot TX ne produit
            # rien — il LIT le shm qu'on lui a câblé, dont le nom dépend du câblage. Le fabriquer
            # ici donnerait un nom qui entre en collision avec celui du RX de même index : une
            # colonne qui ment.
            # ⚠ PASSER PAR `numerotation` (2026-08-19) : ce nom était construit à la main sur
            # l'indice BRUT, donc resté 0-based après la migration du 2026-08-13. Il ne s'agissait
            # pas d'un simple défaut d'affichage — c'est la valeur que l'UI reprend pour CÂBLER un
            # consommateur, donc une porte par laquelle des noms 0-based revenaient s'écrire dans
            # les configs bien après la migration (constaté à Horace : un mur pointait sur
            # `<hn>_0`, un flux qui ne peut plus exister). Le nom d'un flux décide de son UUID
            # (uuid5) : il n'y a pas de « à peu près » possible ici.
            shm = ((flux_video(hn, idx) if ess == "video" else
                    flux_audio(hn, idx) if ess == "audio" else
                    flux_anc(hn, idx)) if role == "rx" else "")
            item = {**f, "role": role, "shm": shm}
            # `alarmes`/`niveau` UNIQUEMENT sur les flux vidéo : ce sont les seuls que le moteur
            # sonde (il y publie aussi le silence de leur audio). Les poser sur un audio ou un ANC
            # afficherait des réglages sans effet — l'UI doit pouvoir distinguer les deux.
            r = table.get((ess, idx))
            if r:
                item["alarmes"] = r["drapeaux"]
                item["niveau"] = r["niveau"]
            out.append(item)
    return jsonify({"ok": True, "vmid": vmid, "hostname": hn, "flows": out,
                    "drapeaux": list(_iof.ALARMES), "niveaux": list(_iof.NIVEAUX)})


def push_probes_all(vmid, params=None):
    """Repousse les sondes armées de TOUS les slots RX d'un moteur. Appelé après un (re)déploiement.

    Le moteur garde `_sig_probes` en RAM : une recréation le ramène à son défaut, qui est de TOUT
    calculer. La surveillance n'est donc jamais perdue par accident — c'est l'ÉCONOMIE qui l'est,
    jusqu'à ce que quelqu'un ré-enregistre un réglage. Ce repush ferme ce trou."""
    from ..database import db_get_container
    from .. import io2110_flows as _iof
    try:
        if params is None:
            dc = _load_dc(db_get_container(vmid)) or {}
            if dc.get("type") != "2110_io":
                return 0
            params = dc.get("params") or {}
        n = 0
        table = _iof.alarmes_par_slot(params, "rx")
        for (ess, idx), cfg in table.items():
            if ess == "video" and _push_probes(vmid, idx, cfg["drapeaux"]):
                n += 1
        return n
    except Exception as e:
        log.info("repush des sondes vmid=%s : %s", vmid, e)
        return 0


def _push_probes(vmid, idx, alarmes):
    """Dit au moteur quelles sondes calculer sur ce slot RX (POST :8081/probes).

    Trois sondes distinctes, parce qu'elles n'ont pas le même coût : `video` (noir + gel — un CRC
    et une moyenne, négligeable), `gamut` (le calcul cher : matriçage BT.709 sur les lignes
    échantillonnées) et `audio` (silence + saturation, lus en continu à 20 Hz)."""
    from ..addressing import get_container_ip
    from .. import deploy
    probes = {"video": bool(alarmes.get("frozen") or alarmes.get("black")),
              "gamut": bool(alarmes.get("gamut")),
              "audio": bool(alarmes.get("silence") or alarmes.get("clip"))}
    try:
        ip = get_container_ip(vmid)
        if not ip:
            return False
        deploy.agent_session().post(deploy.agent_url(ip, "/probes"),
                                    json={"slot": int(idx), "probes": probes}, timeout=5,
                                    headers=deploy.agent_headers(vmid))
        return True
    except Exception as e:
        log.info("push sondes vmid=%s slot=%s : %s (le moteur garde son défaut : tout calculer)",
                 vmid, idx, e)
        return False


@bp.route("/api/mtl/<int:vmid>/flows/alarmes", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_flow_alarmes(vmid):
    """Niveau d'attention d'UNE source : quels drapeaux de présence signal ont le droit d'alerter.
    Body : {role:"rx"|"tx", idx:<n° de slot vidéo>, alarmes:{frozen,black,silence,gamut}, niveau?}.

    Le réglage est porté par la SOURCE VIDÉO, et c'est le moteur qui l'impose : il sonde l'image ET
    le son d'un slot puis publie les deux dans le signal du slot vidéo. Le silence de `_audio_3`
    remonte donc sur `Rx #3`. L'ANC n'a aucune sonde.

    ⚠ La source est désignée par son INDEX DE SLOT, pas par un id de flux. Tant que `rx_flows`
    n'est pas persisté, `active_flows` DÉRIVE la liste et régénère des ids à CHAQUE appel : un
    client qui lirait un id puis le reposterait tomberait systématiquement sur « flux inconnu ».
    L'index, lui, est stable et c'est déjà ce que l'UI affiche (« Rx #n »).

    Pourquoi c'est un réglage PAR SOURCE : un gel n'est un incident que si la source est censée
    bouger. Une mire, une ardoise ou un player en pause sont fixes par construction — alerter
    dessus apprend à l'exploitant à ignorer son fil (constaté sur Horace : un slot a produit 376
    alertes de gel en 19 h). Cf. la règle « pas d'intention, pas d'alarme ».

    PUREMENT DÉCLARATIF : ça ne touche ni le moteur, ni les sessions, ni le script — seulement ce
    que l'orchestrateur accepte de remonter. Donc aucune coupure, aucun redéploiement, et pas de
    pré-confirmation à demander (contrairement à flows/add-remove qui peuvent recréer le moteur)."""
    from ..database import db_get_container, db_update_deploy_config
    from .. import io2110_flows as _iof
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role") or "rx"
    try:
        idx = int(data.get("idx"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "idx manquant ou invalide"}), 400
    if role not in ("rx", "tx"):
        return jsonify({"ok": False, "error": "role invalide"}), 400
    if not isinstance(data.get("alarmes"), dict):
        return jsonify({"ok": False, "error": "alarmes manquant"}), 400
    niveau = str(data.get("niveau") or _iof.NIVEAU_DEFAUT).lower()
    if niveau not in _iof.NIVEAUX:
        return jsonify({"ok": False,
                        "error": "niveau invalide (%s)" % ", ".join(_iof.NIVEAUX)}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dict(dc.get("params") or {})
    cle = "rx_flows" if role == "rx" else "tx_flows"
    # On PERSISTE la liste résolue : sans ça un moteur dont les flux sont encore DÉRIVÉS (jamais
    # édités) perdrait le réglage au prochain calcul — les ids dérivés sont régénérés à chaque appel.
    flows = _iof.active_flows(params, role)
    cible = next((f for f in flows
                  if f.get("essence") == "video" and int(f.get("idx") or 0) == idx), None)
    if cible is None:
        return jsonify({"ok": False, "error": f"aucune source vidéo à l'index {idx}"}), 404
    # Restreint aux drapeaux du RÔLE (cf. io2110_flows.ALARMES_ROLE) : `tx_late` n'a de sens que
    # côté TX (mesure du slot de sortie) — l'accepter sur une source RX persisterait un réglage
    # sans effet, jamais lu (le signal RX n'a jamais cette clé), mais qui polluerait l'API/l'UI.
    permis = _iof.ALARMES_ROLE.get(role, _iof.ALARMES)
    demande = {k: bool(v) for k, v in data["alarmes"].items() if k in permis}
    defaut_role = {k: v for k, v in _iof.ALARMES_DEFAUT.items() if k in permis}
    cible["alarmes"] = {**defaut_role, **demande}
    cible["niveau"] = niveau
    params[cle] = _iof.normalize(flows)
    db_update_deploy_config(vmid, dc.get("type"), params)
    # GATING DU COÛT : le moteur calcule les sondes pour tout le monde tant qu'on ne lui dit pas
    # ce qui est armé (0,03 % d'un cœur par source pour le gamut, 0,09 % par flux pour l'audio).
    # Décocher doit économiser le calcul, pas seulement taire l'alerte. Best-effort et NON bloquant :
    # un moteur injoignable garde son comportement par défaut — tout surveiller — et le réglage sera
    # repoussé au prochain déploiement. Ne jamais échouer la sauvegarde pour un push raté.
    if role == "rx":
        _push_probes(vmid, idx, cible["alarmes"])
    return jsonify({"ok": True, "idx": idx, "alarmes": cible["alarmes"],
                    "niveau": niveau, cle: params[cle]})


@bp.route("/api/mtl/<int:vmid>/flows/remove", methods=["POST"])
@require_perm("containers.deploy")
def api_mtl_flow_remove(vmid):
    """Retire UN flux (par id). Si le flux est une VIDÉO, ses audios/ANC attachés sont retirés avec
    lui. Body : {id}. RX : teardown À CHAUD (_mtl_teardown_rx_flows) → file moteur libérée
    immédiatement via reconcile(), sans réinit ni faute PTP."""
    from ..database import db_get_container
    from .. import io2110_flows as _iof
    data = request.get_json(force=True, silent=True) or {}
    fid = data.get("id")
    if not fid:
        return jsonify({"ok": False, "error": "id manquant"}), 400
    c = db_get_container(vmid)
    if not c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "2110_io":
        return jsonify({"ok": False, "error": "container non 2110_io"}), 400
    params = dict(dc.get("params") or {})
    role, flows, target = None, None, None
    for r in ("rx", "tx"):
        key = "rx_flows" if r == "rx" else "tx_flows"
        fl = _iof.normalize(params.get(key) or _iof.active_flows(params, r))
        t = next((f for f in fl if f["id"] == fid), None)
        if t:
            role, flows, target = r, fl, t
            break
    if not target:
        return jsonify({"ok": False, "error": "flux introuvable"}), 404
    key = "rx_flows" if role == "rx" else "tx_flows"
    removed = [f for f in flows
               if f["id"] == fid or (target["essence"] == "video" and f.get("attached_to") == fid)]
    params[key] = [f for f in flows
                   if f["id"] != fid and not (target["essence"] == "video" and f.get("attached_to") == fid)]
    # RX : teardown à chaud des sessions retirées AVANT le rebuild (le moteur libère la file XDP).
    if role == "rx":
        _mtl_teardown_rx_flows(vmid, removed)
    params = _mtl_apply_flow_change(vmid, params, role, notify=True)
    return jsonify({"ok": True, "rx_flows": params.get("rx_flows"), "tx_flows": params.get("tx_flows"),
                    "note": ("retiré ; file moteur libérée à chaud" if role == "rx" else "retiré")})


# ─── MTL : prép host DPDK/E810 (hôte local uniquement) ───────────────────────
# La prép ne concerne que l'hôte local de cette instance (= proxmox_host) ; il
# n'y a pas de host distant paramétrable (un éventuel onglet « Distant » viendra
# par un autre mécanisme).
@bp.route("/api/mtl/status", methods=["GET"])
@require_login
def mtl_status():
    """État de préparation MTL de l'hôte local (IOMMU, hugepages, ice, vfio, NICs)."""
    from .. import settings as st
    from .. import mtl
    s = mtl.verifier(_req_host())
    s["settings"] = {
        "mtl_hugepages_1g": int(st.get("mtl_hugepages_1g") or 16),
    }
    return jsonify(s)

@bp.route("/api/mtl/apply", methods=["POST"])
@require_perm("settings.edit")
def mtl_apply():
    """Persiste hugepages puis applique la prép (cmdline + vfio + refresh) en local."""
    from ..database import db_set_setting
    from .. import mtl
    data = request.json or {}
    try:
        huge = int(data.get("hugepages_1g"))
    except (TypeError, ValueError):
        huge = 16
    if not 0 <= huge <= 256:
        huge = 16
    db_set_setting("mtl_hugepages_1g", huge)
    ok, msg, reboot_needed = mtl.appliquer(_req_host(), huge)
    code = 200 if ok else 500
    return jsonify({"ok": ok, "msg": msg, "reboot_needed": reboot_needed}), code

@bp.route("/api/mtl/cpufreq", methods=["POST"])
@require_perm("settings.edit")
def mtl_cpufreq():
    """(Ré)épingle la fréquence des cœurs ISOLÉS du moteur 2110 au max du CPU présent, et rend
    l'épinglage PERSISTANT (unité systemd). Séparé de `/api/mtl/apply` parce que ça s'applique
    À CHAUD : pas de cmdline réécrit, donc pas de reboot. Sert à réparer un nœud qui a perdu son
    pin (typiquement après un reboot fait avant que l'unité soit posée)."""
    from .. import mtl
    ok, msg, _ = mtl.ensure_cpufreq_performance(_req_host())
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 500)

@bp.route("/api/mtl/reboot", methods=["POST"])
@require_perm("settings.edit")
def mtl_reboot():
    """Redémarre l'hôte local — exige confirm:true (le reboot n'est jamais automatique)."""
    from .. import mtl
    data = request.json or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "confirmation requise"}), 400
    ok, msg = mtl.redemarrer(_req_host())
    return jsonify({"ok": ok, "msg": msg})


# ─── Qualification de carte (bibliothèque de cartes, cf. docs/chantiers/DPDK_NARROW.md §7) ───────────────────────
@bp.route("/api/nodes/<int:node_id>/qualify-nic", methods=["POST"])
@require_perm("containers.deploy")
def qualify_node_nic(node_id):
    """Qualifie la carte média du nœud : mesure le cap RL TX EFFECTIF (non lisible du PMD ; lu du log
    d'un moteur 2110_io tournant sur la carte en pacing narrow) + device_id/firmware, et écrit le
    profil `nic_profiles` (measured=1). Le profil prime ensuite sur la biblio statique au déploiement."""
    from ..database import db_get_node, db_get_containers
    from .. import nic_qualify, docker_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    ct = next((c for c in db_get_containers()
               if c.get("node_id") == node_id and "2110_io" in (c.get("deploy_config") or "")), None)
    if not ct:
        return jsonify({"ok": False, "error": "aucun moteur 2110_io sur ce nœud — en déployer un en "
                        "pacing narrow pour mesurer la capacité de la carte"}), 409
    name = docker_driver._name(ct["vmid"])
    prof = nic_qualify.qualify_node_via_agent(node, name)
    if isinstance(prof, dict) and prof.get("error"):
        # Moteur non sain (crash-loop) : refus EXPLICITE plutôt qu'un profil garbage écrit en silence.
        db_add_alert("alert.prep.qualification_refusee", "warning", node_id=node.get("id"), kind="prep",
                     params={"n": node.get("name"), "e": prof["error"]})
        return jsonify({"ok": False, "error": prof["error"]}), 409
    if not prof:
        return jsonify({"ok": False, "error": "qualification impossible : device_id ou log de files "
                        "absents (moteur non narrow, ou logs tronqués/rotés)"}), 500
    _card = prof.get("model") or prof["device_id"]
    _fw = prof.get("firmware") or "?"
    if prof.get("rl_tx_cap"):
        db_add_alert("alert.prep.carte_qualifiee", "info", node_id=node.get("id"), kind="prep",
                     params={"card": _card, "cap": prof["rl_tx_cap"], "fw": _fw})
    else:
        # Capacité NON mesurable (pas de clamp du PMD) : PTP/DDP sont enregistrés, le cap reste celui
        # de la bibliothèque. Dit explicitement — un « qualifié » muet sur ce point laisserait croire
        # que le cap affiché vient d'une mesure.
        _raison = prof.get("cap_reason")
        if _raison:
            db_add_alert("alert.prep.carte_qualifiee_sans_mesure", "warning", node_id=node.get("id"),
                         kind="prep", params={"card": _card, "fw": _fw, "raison": _raison})
        else:
            db_add_alert("alert.prep.carte_qualifiee_raison_inconnue", "warning", node_id=node.get("id"),
                         kind="prep", params={"card": _card, "fw": _fw})
    return jsonify({"ok": True, "profile": prof})


@bp.route("/api/nodes/<int:node_id>/qualify-cpu", methods=["POST"])
@require_perm("containers.deploy")
def qualify_node_cpu(node_id):
    """Relève l'identité CPU du nœud + un micro-banc mono-cœur, et alimente `cpu_profiles`.

    ★ N'écrit JAMAIS de quota autoritaire : le micro-banc est un PROXY, pas une mesure de scheduler
    chargé jusqu'au décrochage (cf. app/cpu_qualify.py). Le quota effectif reste celui de la cascade
    `docker_driver.sch_quota_mbs` tant qu'aucune campagne de charge n'a ancré le profil."""
    from ..database import db_get_node
    from .. import cpu_qualify as _cq
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    r = _cq.qualify_node_cpu(dict(node))
    if not r.get("ok"):
        return jsonify(r), 500
    db_add_alert(
        "alert.prep.cpu_qualifie", "info", node_id=node_id, kind="prep",
        params={"n": node.get("name"), "model": r["cpu_model"],
                "cores": (r["profil"] or {}).get("cores"),
                "threads": (r["profil"] or {}).get("threads"),
                "memcpy": float((r["profil"] or {}).get("memcpy_gbps") or 0),
                "estim": (r.get("estimation") or {}).get("valeur")})
    return jsonify(r)


@bp.route("/api/cpu-profiles", methods=["GET"])
@require_login
def list_cpu_profiles():
    """Bibliothèque de profils CPU (quota de scheduler par modèle). `measured=0` = déclaratif."""
    from ..database import db_all_cpu_profiles
    return jsonify({"ok": True, "profiles": db_all_cpu_profiles()})


@bp.route("/api/nic-profiles", methods=["GET"])
@require_login
def list_nic_profiles():
    """Bibliothèque de cartes : profils enregistrés (mesurés par la qualification). Cf. docs/chantiers/DPDK_NARROW.md §7."""
    from ..database import db_all_nic_profiles
    return jsonify({"ok": True, "profiles": db_all_nic_profiles()})


# ─── Bibliothèque de MODÈLES de carte 2110 (gabarits par TYPE de carte) ───────────────────────────
# Deux temps, deux objets (cf. app/tx_card_models.py) :
#   1. on RÈGLE des modèles par TYPE de carte — une DÉCLARATION, elle ne coûte RIEN (aucun matériel
#      touché) → édition ADMIN (settings.edit) ;
#   2. on les APPLIQUE à une carte réelle (page Interfaces) — c'est LÀ que le coût se paie (en DPDK,
#      recalcul de l'arbre de pacing ; en AF-XDP : aucun commit possible, donc GRATUIT).
# Le modèle est une SOURCE ; la VÉRITÉ reste le layout appliqué de la carte (io2110_layouts).

@bp.route("/api/tx-card-models", methods=["GET"])
@require_login
def api_tx_card_models():
    """Bibliothèque : modèles + types de carte connus (avec leur plafond de files RL)."""
    from .. import tx_card_models as _tcm
    return jsonify({"ok": True, "models": _tcm.list_models(), "types": _tcm.card_types()})


@bp.route("/api/tx-card-models", methods=["POST"])
@require_perm("settings.edit")
def api_tx_card_model_create():
    from .. import tx_card_models as _tcm
    from ..auth import current_user
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "nom requis"}), 400
    actor = (current_user() or {}).get("username") or ""
    ok, mid, check = _tcm.save_model(None, name=name, nic_model=(d.get("nic_model") or "").strip(),
                                     slots=d.get("slots") or [], notes=d.get("notes") or "",
                                     actor=actor)
    if not ok:
        return jsonify({"ok": False, "error": "; ".join(check["errors"]), "check": check}), 400
    return jsonify({"ok": True, "id": mid, "check": check})


@bp.route("/api/tx-card-models/<int:mid>", methods=["POST"])
@require_perm("settings.edit")
def api_tx_card_model_update(mid):
    """Mise à jour (nom / type de carte / sorties / notes). Un gabarit qui ne tient pas dans le type
    est REFUSÉ avec la raison (jamais de refus muet)."""
    from .. import tx_card_models as _tcm
    from ..auth import current_user
    d = request.get_json(silent=True) or {}
    actor = (current_user() or {}).get("username") or ""
    # Nom vide = refus EXPLICITE (l'UI le dit déjà avant le clic ; ici on empêche l'écriture d'un
    # modèle anonyme, y compris par un client qui court-circuiterait l'UI).
    if isinstance(d.get("name"), str) and not d["name"].strip():
        return jsonify({"ok": False, "error": "nom requis : un modèle sans nom est introuvable "
                                              "dans la bibliothèque"}), 400
    ok, _id, check = _tcm.save_model(
        mid,
        name=(d["name"].strip() if isinstance(d.get("name"), str) else None),
        nic_model=(d["nic_model"].strip() if isinstance(d.get("nic_model"), str) else None),
        slots=(d["slots"] if isinstance(d.get("slots"), list) else None),
        notes=(d["notes"] if isinstance(d.get("notes"), str) else None), actor=actor)
    if not ok:
        return jsonify({"ok": False, "error": "; ".join(check["errors"]), "check": check}), 400
    return jsonify({"ok": True, "id": mid, "check": check})


@bp.route("/api/tx-card-models/cards", methods=["GET"])
@require_login
def api_tx_card_models_cards():
    """Cartes média 2110 du cluster (avec le nombre de sorties capturables) — amorçage de la
    bibliothèque : « créer un modèle À PARTIR d'une carte existante ». LECTURE SEULE."""
    from .. import tx_card_models as _tcm
    return jsonify({"ok": True, "cards": _tcm.cards_inventory()})


@bp.route("/api/tx-card-models/capture", methods=["POST"])
@require_perm("settings.edit")
def api_tx_card_model_capture():
    """Capture les sorties RÉELLES d'une carte dans un NOUVEAU modèle (pré-rattaché à son type) et lie
    la carte à ce modèle. ⚠ C'est une LECTURE de la carte : rien n'est écrit sur elle, aucun commit,
    aucun redéploiement."""
    from .. import tx_card_models as _tcm
    from ..auth import current_user
    d = request.get_json(silent=True) or {}
    node_id, iface = d.get("node_id"), (d.get("iface") or "").strip()
    if not node_id or not iface:
        return jsonify({"ok": False, "error": "node_id et iface requis"}), 400
    ok, res, check = _tcm.capture_card(int(node_id), iface, (d.get("name") or "").strip(),
                                       (current_user() or {}).get("username") or "")
    if not ok:
        return jsonify({"ok": False, "error": res, "check": check}), 400
    return jsonify({"ok": True, "id": res, "check": check})


@bp.route("/api/tx-card-models/<int:mid>/duplicate", methods=["POST"])
@require_perm("settings.edit")
def api_tx_card_model_duplicate(mid):
    from .. import tx_card_models as _tcm
    from ..auth import current_user
    new_id = _tcm.duplicate_model(mid, (current_user() or {}).get("username") or "")
    if not new_id:
        return jsonify({"ok": False, "error": "modèle introuvable"}), 404
    return jsonify({"ok": True, "id": new_id})


@bp.route("/api/tx-card-models/<int:mid>", methods=["DELETE"])
@require_perm("settings.edit")
def api_tx_card_model_delete(mid):
    """Supprime le modèle. Les cartes qui en sont issues GARDENT leur layout (le modèle est une
    source, pas la vérité) — leur rattachement s'affichera « modèle supprimé »."""
    from ..database import db_delete_tx_card_model
    db_delete_tx_card_model(mid)
    return jsonify({"ok": True})


# ─── Application d'un modèle à une CARTE réelle (page Interfaces) ─────────────────────────────────

@bp.route("/api/nodes/<int:node_id>/tx-model/candidates", methods=["GET"])
@require_login
def api_tx_model_candidates(node_id):
    """Modèles applicables à cette carte (+ ceux qui ne le sont PAS, avec la raison) et rattachement
    courant (modèle source, divergence éventuelle)."""
    from .. import tx_card_models as _tcm
    iface = (request.args.get("iface") or "").strip()
    if not iface:
        return jsonify({"ok": False, "error": "paramètre iface requis"}), 400
    from .. import io2110_layouts as _lay
    eng = _lay.engine_for_card(node_id, iface)
    budget = _lay.nic_budget(node_id, iface)
    return jsonify({"ok": True, "models": _tcm.compatible_models(node_id, iface),
                    "binding": _tcm.card_binding(node_id, iface),
                    # Le champ « Modèle » de la fiche d'interface a besoin du moteur (pour provisionner)
                    # et du MODE du port (`rl`) : en AF-XDP, appliquer ne coûte RIEN — il doit le DIRE.
                    "engine": ({"vmid": eng["vmid"], "hostname": eng.get("hostname")} if eng else None),
                    "port": {"pmd": budget.get("pmd"), "rl": bool(budget.get("dpdk_active")),
                             "model": budget.get("model") or ""}})


@bp.route("/api/nodes/<int:node_id>/tx-model/preview", methods=["POST"])
@require_login
def api_tx_model_preview(node_id):
    """Diff + coût AVANT le clic : ce que l'application de ce modèle changerait sur la carte, combien
    de sessions seraient créées, et QUELLES sorties actives figeraient. Ne persiste rien."""
    from .. import tx_card_models as _tcm
    d = request.get_json(silent=True) or {}
    iface = (d.get("iface") or "").strip()
    mid = d.get("model_id")
    if not iface or not mid:
        return jsonify({"ok": False, "error": "iface et model_id requis"}), 400
    prev = _tcm.apply_preview(node_id, iface, int(mid))
    if prev is None:
        return jsonify({"ok": False, "error": "modèle introuvable"}), 404
    return jsonify({"ok": True, "preview": prev})


@bp.route("/api/nodes/<int:node_id>/tx-model/apply", methods=["POST"])
@require_perm("settings.edit")
def api_tx_model_apply(node_id):
    """Écrit le layout DÉCLARÉ de la carte depuis le modèle (+ mémorise le rattachement).
    ⚠ Ne touche PAS le moteur : provisionner les sessions reste l'action explicite
    `/api/mtl/<vmid>/tx-layout/apply` (gatée fenêtre de maintenance) — c'est elle qui coûte."""
    from .. import tx_card_models as _tcm
    from ..auth import current_user
    d = request.get_json(silent=True) or {}
    iface = (d.get("iface") or "").strip()
    mid = d.get("model_id")
    if not iface or not mid:
        return jsonify({"ok": False, "error": "iface et model_id requis"}), 400
    ok, res = _tcm.apply_model_to_card(node_id, iface, int(mid),
                                       (current_user() or {}).get("username") or "")
    if not ok:
        return jsonify({"ok": False, "error": res}), 400
    return jsonify({"ok": True, **res})


# ─── Layouts TX déclarés par NIC (docs/reference/TX_LAYOUTS.md, étage 1 — arbre TX statique) ──────────────────────
# Persistance : app/io2110_layouts.py (blob JSON par node_id+iface, table settings générique). Édition
# ADMIN (settings.edit, cf. docs/reference/TX_LAYOUTS.md décision #1 — le layout vit dans Réglages, adossé à la
# bibliothèque de cartes) ; lecture ouverte à tout connecté (Destinations 2110 l'affiche en lecture
# seule) ; « appliquer » = événement de maintenance sur un moteur déployé (containers.deploy).

@bp.route("/api/nodes/<int:node_id>/tx-layout", methods=["GET"])
@require_login
def api_tx_layout_get(node_id):
    from flask import request as _rq
    from .. import io2110_layouts as _lay
    iface = (_rq.args.get("iface") or "").strip()
    if not iface:
        return jsonify({"ok": False, "error": "paramètre iface requis"}), 400
    layout = _lay.get_layout(node_id, iface)
    budget = _lay.nic_budget(node_id, iface)
    check = _lay.validate_slots(node_id, iface, layout.get("slots"))
    return jsonify({"ok": True, "layout": layout, "budget": budget, "check": check,
                    "presets": _lay.presets_for(node_id, iface)})


@bp.route("/api/nodes/<int:node_id>/tx-layout", methods=["POST"])
@require_perm("settings.edit")
def api_tx_layout_set(node_id):
    from .. import io2110_layouts as _lay
    from ..auth import current_user
    data = request.json or {}
    iface = (data.get("iface") or "").strip()
    if not iface:
        return jsonify({"ok": False, "error": "paramètre iface requis"}), 400
    slots = data.get("slots")
    if slots == [] or slots is None and data.get("clear"):
        _lay.delete_layout(node_id, iface)
        return jsonify({"ok": True, "layout": _lay.get_layout(node_id, iface)})
    try:
        actor = (current_user() or {}).get("username")
    except Exception:
        actor = None
    ok, check = _lay.set_layout(node_id, iface, slots, actor=actor)
    if not ok:
        return jsonify({"ok": False, "error": "budget dépassé", "check": check}), 400
    return jsonify({"ok": True, "layout": _lay.get_layout(node_id, iface), "check": check})


@bp.route("/api/nodes/<int:node_id>/tx-model", methods=["GET"])
@require_login
def api_tx_model(node_id):
    """« Modèle d'utilisation » d'une carte (node_id, iface) : layout déclaré + état RÉEL de chaque
    sortie (active / déclarée-silencieuse / non provisionnée) + source câblée + mode de pacing du
    port + budget + bac de maintenance. UN appel pour toute la vue (cf. io2110_layouts.card_model)."""
    from .. import io2110_layouts as _lay
    iface = (request.args.get("iface") or "").strip()
    if not iface:
        return jsonify({"ok": False, "error": "paramètre iface requis"}), 400
    return jsonify({"ok": True, "model": _lay.card_model(node_id, iface)})


@bp.route("/api/nodes/<int:node_id>/tx-layout/verdict", methods=["POST"])
@require_login
def api_tx_layout_verdict(node_id):
    """Coût d'un layout BROUILLON, AVANT enregistrement/application : combien de sessions vidéo
    seraient créées (= combien de recalages d'arbre) et QUELLES sorties actives figeraient (~1 s).
    N'applique rien, ne persiste rien. `verdict:null` = aucun moteur déployé sur cette carte."""
    from .. import io2110_layouts as _lay
    data = request.get_json(silent=True) or {}
    iface = (data.get("iface") or "").strip()
    if not iface:
        return jsonify({"ok": False, "error": "paramètre iface requis"}), 400
    slots = data.get("slots") or []
    try:
        verdict = _lay.draft_verdict(node_id, iface, slots)
    except Exception as e:
        log.warning("tx-layout verdict %s/%s: %s", node_id, iface, e)
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "verdict": verdict,
                    "check": _lay.validate_slots(node_id, iface, slots)})


@bp.route("/api/mtl/<int:vmid>/tx-layout/status", methods=["GET"])
@require_login
def api_tx_layout_status(vmid):
    """État du layout TX pour ce moteur (lecture seule — Destinations 2110).

    `cards` = état PAR UNITÉ DE CAPACITÉ (port autonome ou paire 2022-7). `status` reste l'état de la
    carte primaire, pour les appelants historiques — mais sur un nœud bi-port il ne décrit que la
    moitié des sorties : la seconde carte y était tout simplement invisible."""
    from .. import io2110_layouts as _lay
    from ..database import db_get_node
    from .. import docker_driver as _dd
    node_id, units = _lay.engine_units(vmid)
    cards = []
    for u in units:
        st = _lay.layout_status(vmid, u["key"])
        st.update({"key": u["key"], "label": u["label"], "kind": u["kind"], "ifaces": u["ifaces"]})
        cards.append(st)
    return jsonify({"ok": True, "status": _lay.layout_status(vmid), "cards": cards})


@bp.route("/api/mtl/<int:vmid>/tx-layout/apply", methods=["POST"])
@require_perm("containers.deploy")
def api_tx_layout_apply(vmid):
    """Applique le layout déclaré de la NIC du moteur : auto-alloue les destinations manquantes,
    persiste `tx_slots` et pousse au contrôleur (`provisioned=True` par slot déclaré, cf.
    docker_driver.push_tx_slots). ÉVÉNEMENT DE MAINTENANCE (crée les sessions manquantes → recalcule
    l'arbre RL du port) → gaté par l'étage 2 : confirmation nommant les sorties qui vont figer, ou
    report dans la fenêtre de maintenance (`defer:true`)."""
    from .. import io2110_layouts as _lay
    import json as _json
    from ..database import db_get_container
    data = request.get_json(silent=True) or {}
    # Le layout déclaré fait AUTORITÉ sur le nombre de sorties. S'il en déclare plus que le budget
    # bootté du moteur (ACTIVE_TX_COUNT figé au `docker run`), l'appliquer RECRÉE le moteur pour
    # ré-réserver les files RL (coupure brève de TOUS les flux, RX inclus) → verdict engine-scope +
    # confirmation, comme « + Ajouter un TX » au-delà de la réserve. apply_layout porte alors
    # active_tx_count au nombre déclaré et recrée lui-même.
    # Carte CIBLE : celle cliquée (multi-port), à défaut la PRIMAIRE.
    # ⚠ NE JAMAIS retomber sur « toutes les cartes » ici : cette route sert un geste PAR CARTE, et
    # `apply_layout(iface=None)` applique le nœud entier. Le 2026-07-27, la page Modèles postant un
    # corps vide, un clic sur une carte a additionné les modèles des deux (32+32 → 64 sorties), saturé
    # les 63 files RL du port et tué les 6 RX. `iface=None` reste réservé au déploiement (resync).
    node_id, iface = _lay.layout_iface_for_container(vmid)
    _req_iface = (data.get("iface") or "").strip() or iface
    # Total DÉCLARÉ après cet apply = ce que produirait l'allocateur (somme des modèles, les cartes
    # non ciblées conservant leurs sorties) — PAS le seul compte de la carte cliquée.
    n_decl = _lay.planned_active_tx(vmid, iface) if iface else 0
    booted = _engine_booted_active_tx(vmid)
    if booted is not None and n_decl != booted:
        from .. import tx_maintenance as _txm
        c = db_get_container(vmid) or {}
        try:
            params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:
            params = {}
        verdict = _txm.classify(vmid, _lay.preview_layout_params(vmid, params), op="recreate")
        verdict["reason"] = ("le modèle déclare %d sortie(s) (moteur : %d) — le moteur sera "
                             "RECRÉÉ pour aligner les sorties (coupure brève de tous les flux)"
                             % (n_decl, booted))
        if bool(data.get("defer")):
            _g, _ = _tx_gate(vmid, "tx_layout_apply", {"iface": _req_iface}, data)   # réutilise le report en fenêtre
            if _g is not None:
                return _g
        elif not bool(data.get("confirm")):
            return jsonify({"ok": False, "needs_confirm": True, "verdict": verdict,
                            "reason": verdict.get("reason")}), 409
        with verrou_vmid(vmid, op="tx-layout-apply"):
            # `redeploy` = l'exploitant a choisi « appliquer ET redéployer maintenant ». Sinon on
            # écrit la déclaration et reconcile_engine_sizing signale le redéploiement requis.
            ok, result = _lay.apply_layout(vmid, iface=_req_iface,
                                           redeploy=bool(data.get("redeploy")))
        if not ok:
            return jsonify({"ok": False, "error": result}), 400
        return jsonify({"ok": True, "verdict": verdict, **result})
    # Chemin à chaud (le layout tient dans le budget bootté) : gate tx_layout_apply existant.
    _gate, _verdict = _tx_gate(vmid, "tx_layout_apply", {"iface": _req_iface}, data)
    if _gate is not None:
        return _gate
    with verrou_vmid(vmid, op="tx-layout-apply"):
        ok, result = _lay.apply_layout(vmid, iface=_req_iface,
                                       redeploy=bool(data.get("redeploy")))
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "verdict": _verdict, **result})


@bp.route("/api/mtl/<int:vmid>/tx/mcast-plan", methods=["POST"])
@require_perm("containers.deploy")
def api_tx_mcast_plan(vmid):
    """Re-planifie les adresses multicast des sorties TX : UNE ADRESSE DE GROUPE PAR FLUX, déduite
    du rang (cf. allocations._plan_offset). Sans `apply:true` → SIMULATION (diff seul, aucune
    réservation, aucun push). Avec → événement de maintenance (la destination entre dans la
    signature de session : commit TM = un blip), donc gaté étage 2 comme les autres actions TX,
    et différable dans la fenêtre de maintenance (`defer:true`)."""
    from .. import allocations as _alloc
    data = request.get_json(silent=True) or {}
    if not bool(data.get("apply")):
        diff, err = _alloc.plan_tx_multicast(vmid, appliquer=False)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "dry_run": True, "diff": diff,
                        "changes": sum(1 for d in diff if d["etat"] == "a_changer")})
    _gate, _verdict = _tx_gate(vmid, "tx_mcast_plan", {}, data)
    if _gate is not None:
        return _gate
    with verrou_vmid(vmid, op="tx-mcast-plan"):
        diff, err = _alloc.plan_tx_multicast(vmid, appliquer=True)
    if err:
        return jsonify({"ok": False, "error": err, "diff": diff}), 400
    return jsonify({"ok": True, "verdict": _verdict, "diff": diff,
                    "applied": sum(1 for d in diff if d["etat"] == "applique"),
                    "conflicts": [d for d in diff if d["etat"] == "conflit"]})


# ─── Étage 2 : classification des actions + fenêtre de maintenance (docs/reference/TX_LAYOUTS.md) ─────────────────
# Modèle : app/tx_maintenance.py. Le verdict est CALCULÉ (diff des signatures de sessions TX
# réellement poussées au contrôleur), pas codé en dur — et dépend du MODE DU PORT : sur af_xdp il n'y
# a pas de rate limiter, donc pas de commit TM, donc AUCUNE action n'est perturbatrice.

@bp.route("/api/mtl/<int:vmid>/tx-preflight", methods=["POST"])
@require_perm("containers.deploy")
def api_tx_preflight(vmid):
    """Verdict d'une action AVANT de la lancer (colore le contrôle : vert = immédiat et sans effet,
    orange = recale l'arbre) et NOMME les sorties qui figeraient. N'applique RIEN.
    Body : {op, args}. `op` vide → verdict générique du moteur (mode du port + sorties actives)."""
    from .. import tx_maintenance as _txm
    data = request.get_json(silent=True) or {}
    op = (data.get("op") or "").strip()
    args = data.get("args") or {}
    try:
        params_after = _txm.preview(vmid, op, args) if op else None
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if params_after is None:
        from ..database import db_get_container
        import json as _json
        c = db_get_container(vmid) or {}
        try:
            params_after = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:
            params_after = {}
    return jsonify({"ok": True, "verdict": _txm.classify(vmid, params_after, op=op or "tx_edit")})


@bp.route("/api/tx-maintenance", methods=["GET"])
@require_login
def api_tx_maintenance_list():
    """Bac des changements TX en attente (tous moteurs, ou `?vmid=`)."""
    from .. import tx_maintenance as _txm
    vmid = request.args.get("vmid")
    return jsonify({"ok": True,
                    "pending": _txm.list_pending(int(vmid) if vmid else None)})


@bp.route("/api/tx-maintenance/<int:pid>", methods=["DELETE"])
@require_perm("containers.deploy")
def api_tx_maintenance_cancel(pid):
    """Annule UN changement en attente (rien n'avait été poussé au moteur)."""
    from .. import tx_maintenance as _txm
    _txm.cancel(pid)
    return jsonify({"ok": True})


@bp.route("/api/mtl/<int:vmid>/tx-maintenance/apply", methods=["POST"])
@require_perm("containers.deploy")
def api_tx_maintenance_apply(vmid):
    """Applique le bac d'un moteur. Body : {at:"YYYY-MM-DDTHH:MM"} → PLANIFIE au lieu d'appliquer.
    Sans `at` : application immédiate, EN UN SEUL LOT (un seul recalcul d'arbre = un seul blip)."""
    from .. import tx_maintenance as _txm
    from ..auth import current_user
    data = request.get_json(silent=True) or {}
    at = (data.get("at") or "").strip()
    if at:
        n = _txm.schedule(vmid, at)
        return jsonify({"ok": True, "scheduled": n, "at": at,
                        "pending": _txm.list_pending(vmid)})
    try:
        actor = (current_user() or {}).get("username")
    except Exception:
        actor = ""
    n, errors = _txm.apply_pending(vmid, actor=actor)
    return jsonify({"ok": not errors, "applied": n, "errors": errors,
                    "pending": _txm.list_pending(vmid)})
