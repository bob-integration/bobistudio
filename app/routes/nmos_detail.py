# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Vues détaillées NMOS/2110 : agrégation par container des receivers/senders IS-04/05 +
fps live (:8080), résolution multi-NIC des ports média, et la vue I/O 2110 (/api/io/mtl)
qui alimente les onglets Sources/Destinations 2110.

Regroupe deux blocs qui vivaient séparés sans rapport avec leur en-tête de section
(receivers/io_mtl dans un bloc « Split/SuperSource » vidé de son contenu ; senders dans
« Share links WebRTC ») — c'est le même domaine (agrégation NMOS) une fois réuni.

Dépendance à double sens avec mtl_engine.py (le moteur 2110_io lui-même) : ce module
importe _mtl_media_port_count/_mtl_active_caps de mtl_engine.py (budgets de files) ;
mtl_engine.py importe en retour _compute_receivers_detail/_io_media_ports d'ici (détection
d'opération disruptive). Aucune circularité réelle : cette importation-ci est à un niveau
module (immédiate), celle de mtl_engine.py est locale à ses fonctions (différée à l'appel,
donc après que les deux modules soient chargés) — .mtl_engine DOIT être importé avant ce
module dans app/routes/__init__.py."""

from flask import jsonify, request
from ..numerotation import (cle_tx_shm, cle_tx_audio_shm, cle_tx_anc_shm, slot_tx, slot_rx,
                           flux_video, flux_audio, flux_anc)

from . import bp
from .shared import _mtl_total_queues
from .mtl_engine import _mtl_media_port_count, _mtl_active_caps, _mtl_rl_tx_budget
from ..auth import require_login, require_perm
from ..database import db_get_containers


def _fmt_from_sdp(sdp):
    """Extrait {width,height,fps,scan} du fmtp ST 2110-20 d'un SDP (a=fmtp:… width=…; height=…;
    exactframerate=N[/D]; interlace). {} si rien d'exploitable. Source de vérité d'un flux actif."""
    if not sdp:
        return {}
    import re as _re
    out = {}
    mw = _re.search(r"width=(\d+)", sdp)
    mh = _re.search(r"height=(\d+)", sdp)
    mf = _re.search(r"exactframerate=(\d+)(?:/(\d+))?", sdp)
    if mw: out["width"] = int(mw.group(1))
    if mh: out["height"] = int(mh.group(1))
    if mf:
        num = int(mf.group(1)); den = int(mf.group(2)) if mf.group(2) else 1
        out["fps"] = round(num / den, 2)
    # Scan EXPLICITE depuis le SDP : « interlace » présent ⇒ i, sinon p. On le pose toujours (≠ None)
    # pour que la topologie/Sources ne retombe PAS sur le scan CONFIGURÉ (dc_params) d'un flux actif
    # (un flux 1080p s'affichait « 1080i » à cause du repli sur la config).
    if mw or mh:
        out["scan"] = "i" if _re.search(r"\binterlace\b", sdp) else "p"
    # Reste du fmtp ST 2110-20 : chroma (sampling), profondeur, colorimétrie, transfert (HDR), range.
    ms = _re.search(r"sampling=YCbCr-(\d:\d:\d)", sdp) or _re.search(r"sampling=(RGB|YCbCr-[\w:]+)", sdp)
    if ms:
        out["chroma"] = ms.group(1)
    md = _re.search(r"depth=(\d+)", sdp)
    if md: out["bit_depth"] = int(md.group(1))
    mc = _re.search(r"colorimetry=([\w.\-]+)", sdp)
    if mc: out["colorimetry"] = mc.group(1)
    mt = _re.search(r"\bTCS=([\w.\-]+)", sdp)
    if mt: out["tcs"] = mt.group(1)
    mr = _re.search(r"\bRANGE=([\w.\-]+)", sdp)
    if mr: out["range"] = mr.group(1)
    return out


def _audio_fmt_from_sdp(sdp):
    """Extrait {bit_depth, sample_rate, channels} du rtpmap ST 2110-30 d'un SDP
    (a=rtpmap:<pt> L24/48000/8 → 24 bits, 48 kHz, 8 canaux). {} si rien d'exploitable."""
    if not sdp:
        return {}
    import re as _re
    m = _re.search(r"a=rtpmap:\d+\s+L(\d+)/(\d+)(?:/(\d+))?", sdp)
    if not m:
        return {}
    return {"bit_depth": int(m.group(1)), "sample_rate": int(m.group(2)),
            "channels": int(m.group(3)) if m.group(3) else 1}


def _io_media_ports(node, params, pins_key):
    """Résout les ports média d'un nœud + l'assignation EFFECTIVE par slot (auto modulo / épinglé)
    pour un moteur 2110_io. Réutilisé par les vues I/O 2110 (RX `rx_pins` et TX `tx_pins`).
    Retourne (multi, ports_meta, auto_ports, slot_port_fn). Mono-port → (False, [], [], lambda→None)."""
    from .. import docker_driver as _dd
    _mifs = _dd._media_ifaces(node) if node else []
    if len(_mifs) <= 1:
        return False, [], [], (lambda i: None)
    # Résolution slot→port : docker_driver.engine_slot_ports est LA source unique (miroir du
    # controller). Cette vue n'ajoute que l'habillage humain (réseau, alias, drapeau `pinned`).
    auto_ports, _resolve = _dd.engine_slot_ports(node, params, pins_key)
    from ..database import db_get_media_networks as _dgmn
    _net_name = {n["id"]: n["name"] for n in _dgmn()}
    # Alias humain par interface (node_interfaces.alias, ex. « PGM-Rouge ») — clé = ifname (netdev).
    _alias_by_if = {}
    if node and node.get("id"):
        from ..database import db_get_node_interfaces as _dgni
        for _r in (_dgni(node["id"]) or []):
            if _r.get("alias") and _r.get("ifname"):
                _alias_by_if[_r["ifname"]] = _r["alias"]
    ports_meta = [{"ifname": e["ifname"], "network": _net_name.get(e.get("network_id")),
                   "alias": _alias_by_if.get(e["ifname"])}
                  for e in _mifs]
    _declared = {e["ifname"] for e in _mifs}
    _pins = params.get(pins_key) or {}
    def _slot_port(i):
        _if = _resolve(i)
        return {"iface": _if, "pinned": _pins.get(str(i)) in _declared,
                "alias": _alias_by_if.get(_if)}
    return True, ports_meta, auto_ports, _slot_port


def _compute_receivers_detail(only_vmid=None):
    """Agrège pour chaque container NMOS : ses receivers (IS-04 IDs + state IS-05),
    + un fetch live des fps par pipeline depuis le /metrics du container.
    `only_vmid` (int) → limite au seul container (endpoint par-vmid du shell Sources)."""
    from services import nmos as _nmos
    from ..addressing import get_container_ip
    from ..metrics import rx_stalled_cache as _rx_stalled_d
    import requests as _req

    out = []
    containers = db_get_containers()
    by_vmid = {c["vmid"]: c for c in containers}

    with _nmos._lock:
        recv_snapshot = list(_nmos._receivers.values())
        state_snapshot = {k: v for k, v in _nmos._recv_state.items()}

    by_container = {}  # vmid → list de receivers
    for r in recv_snapshot:
        # tag vmid absent/« None » (receiver mal enregistré) → on ignore au lieu de 500 (cf. senders).
        _vtag = (r.get("tags") or {}).get("urn:x-mxl:vmid") or [0]
        try:
            vmid = int(_vtag[0])
        except (ValueError, TypeError, IndexError):
            continue
        if only_vmid is not None and vmid != only_vmid:
            continue
        by_container.setdefault(vmid, []).append(r)

    import json as _json
    for vmid, recvs in sorted(by_container.items()):
        c = by_vmid.get(vmid, {})
        ip = c.get("ip") or get_container_ip(vmid)
        hostname = c.get("hostname") or f"container_{vmid}"
        # deploy_config → état du générateur par slot
        dc = c.get("deploy_config")
        try:
            dc = _json.loads(dc) if isinstance(dc, str) else dc
        except Exception:
            dc = None
        dc_params = (dc or {}).get("params") or {}
        # Multi-NIC : ports média du nœud + assignation effective par slot (auto modulo / épinglé).
        # Calculé seulement si le nœud a ≥2 ports (sinon mono-port → pas de sélecteur côté UI).
        from ..database import db_get_node as _dgn
        _node = _dgn(c.get("node_id")) if c.get("node_id") else None
        _multi, _ports_meta, _auto_ports, _slot_port_fn = _io_media_ports(_node, dc_params, "rx_pins")
        def _slot_port(i):
            return _slot_port_fn(i)
        sim_master = bool(dc_params.get("sim_master", dc_params.get("simulation")))
        v_slots_cfg = dc_params.get("sim_video_slots") or []
        a_slots_cfg = dc_params.get("sim_audio_slots") or []
        # Association audio→vidéo : audio_count = video_count × audio_per_video, les audios
        # étant rangés par blocs (vidéo 0 : audios 0..aper-1, vidéo 1 : aper..2aper-1, …).
        # Fallback si audio_per_video absent (vieux containers) : division entière exacte.
        n_video_cfg = int(dc_params.get("video_count") or 0)
        n_audio_cfg = int(dc_params.get("audio_count") or 0)
        aper = int(dc_params.get("audio_per_video") or 0)
        if aper <= 0 and n_video_cfg > 0 and n_audio_cfg % n_video_cfg == 0:
            aper = n_audio_cfg // n_video_cfg
        # Groupement audio/ANC→vidéo (« Option A ») : piloté par rx_flows (attached_to) ; repli
        # dérivé du legacy si la liste est absente. Cartes (essence, idx) → idx vidéo / rang.
        from .. import io2110_flows as _iof
        _rx_flows = _iof.active_flows(dc_params, "rx")
        _rx_vid_of, _rx_sub_of = _iof.grouping_maps(_rx_flows)
        _rx_id_of = {(f["essence"], f["idx"]): f["id"] for f in _rx_flows}
        # Fetch live fps par pipeline (clé (essence, idx) — vidéo et audio peuvent partager un idx)
        per_key_fps = {}
        nic_info = {}
        data = {}   # réponse :8080 — initialisée AVANT le fetch : si le container est injoignable
                    # (down, IP changée, timeout), on dégrade proprement (xdp/nic vides) au lieu de
                    # lever un NameError plus bas sur data.get("xdp") → 500 qui casse Sources + Destinations.
        if ip:
            try:
                rr = _req.get(f"http://{ip}:8080", timeout=1.5)
                if rr.status_code == 200:
                    data = rr.json() or {}
                    for it in data.get("receivers", []):
                        key = (it.get("essence", "video"), int(it.get("idx", -1)))
                        per_key_fps[key] = it
                    nic_info = data.get("nic") or {}
            except Exception:
                pass

        receivers_out = []
        def _sort_recv(x):
            st = state_snapshot.get(x["id"], {})
            ess = st.get("essence", "video")
            return (0 if ess == "video" else 1,
                    int((x.get("tags") or {}).get("urn:x-mxl:receiver_index", [0])[0]))
        for r in sorted(recvs, key=_sort_recv):
            rid = r["id"]
            idx = int((r.get("tags") or {}).get("urn:x-mxl:receiver_index", [0])[0])
            state = state_snapshot.get(rid, {})
            essence = state.get("essence", "video")
            active = state.get("active") or {}
            sub = r.get("subscription") or {}
            tp = (active.get("transport_params") or [{}])[0]
            live = per_key_fps.get((essence, idx), {})
            # ⚠ NOM DE FLUX = `numerotation` (2026-08-19) : construit ici sur l'indice BRUT, il
            # restait 0-based après la migration du 2026-08-13 et désignait donc le flux VOISIN.
            shm_name = (flux_audio(hostname, idx) if essence == "audio" else
                        flux_anc(hostname, idx) if essence == "anc" else
                        flux_video(hostname, idx))
            slots_cfg = a_slots_cfg if essence == "audio" else v_slots_cfg
            slot_cfg = slots_cfg[idx] if 0 <= idx < len(slots_cfg) else {}
            simulated = sim_master and bool((slot_cfg or {}).get("enabled"))
            # Détails du générateur local (pour le tooltip riche côté front).
            # Exposé même éteint : le tooltip montre alors les réglages mémorisés.
            if essence == "audio":
                gen = {
                    "freq": (slot_cfg or {}).get("freq"),
                    "level_db": (slot_cfg or {}).get("level_db"),
                    "active": list((slot_cfg or {}).get("active") or []),
                    "rupted": list((slot_cfg or {}).get("rupted") or []),
                }
            else:
                gen = {"pattern": (slot_cfg or {}).get("pattern")}
            # Format affiché : si un SDP est actif, on le lit du SDP (le receiver s'adapte au
            # flux reçu) ; sinon repli sur le deploy_config (format configuré). Vaut pour tous
            # les receivers (LXC ffmpeg comme MTL) — la source de vérité d'un flux actif = son SDP.
            _sdp_data = ((active.get("transport_file") or {}).get("data"))
            _sfmt = _fmt_from_sdp(_sdp_data) if essence == "video" else {}
            _afmt = _audio_fmt_from_sdp(_sdp_data) if essence == "audio" else {}
            receivers_out.append({
                "idx": idx,
                "id": rid,
                "essence": essence,
                "label": r.get("label"),
                "active": bool(sub.get("active")),
                "sender_id": sub.get("sender_id"),
                "multicast_ip": tp.get("multicast_ip"),
                "destination_port": tp.get("destination_port"),
                "shm_path": f"/dev/shm/{shm_name}",
                "simulated": simulated,
                "gen": gen,
                "fps": live.get("fps"),
                "frame_index": live.get("frame_index"),
                # État LIVE du moteur (:8080) : init|mtl|simu|idle|error. `generating` = une mire
                # tourne RÉELLEMENT sur ce slot vidéo (gen explicite) → badge GÉN honnête côté UI
                # (≠ `simulated` qui dérive de la config). Un slot non abonné sans gen = "idle".
                "mode": live.get("mode"),
                "generating": (essence == "video" and live.get("mode") == "simu"),
                # « Abonné mais ne reçoit pas » (vidéo) : mode mtl mais flux figé (échec création
                # st20p_rx_create / budget lcores, OU pas de trafic réseau). Surfacé en badge.
                "rx_stalled": (bool(_rx_stalled_d.get(vmid, {}).get(shm_name)) if essence == "video" else None),
                # Timecode ATC (essence ANC) : décodé par mtl_rx, relayé via les métriques :8080.
                "timecode": (live.get("timecode") if essence == "anc" else None),
                "df":       (bool(live.get("df")) if essence == "anc" else None),
                # Format vidéo : SDP actif prioritaire, repli deploy_config.
                "width":  (int(_sfmt.get("width")  or dc_params.get("width")  or 1280) if essence == "video" else None),
                "height": (int(_sfmt.get("height") or dc_params.get("height") or 720) if essence == "video" else None),
                "scan":   ((_sfmt.get("scan") or dc_params.get("scan")) if essence == "video" else None),
                # Chroma / colorimétrie / transfert(HDR) / range VIDÉO — SDP prioritaire, repli config.
                "chroma":      (_sfmt.get("chroma")      or dc_params.get("chroma")      if essence == "video" else None),
                "colorimetry": (_sfmt.get("colorimetry") or dc_params.get("colorimetry") if essence == "video" else None),
                "tcs":         (_sfmt.get("tcs")         if essence == "video" else None),
                "range":       (_sfmt.get("range")       if essence == "video" else None),
                # Format AUDIO (2110-30) lu du SDP actif : fréquence / nb de canaux.
                "sample_rate": (_afmt.get("sample_rate") if essence == "audio" else None),
                "channels":    (_afmt.get("channels")    if essence == "audio" else None),
                # Profondeur de bits : audio (L24…) depuis le SDP audio ; vidéo (depth=) SDP→config.
                "bit_depth":   (_afmt.get("bit_depth") if essence == "audio"
                                else (_sfmt.get("bit_depth") or dc_params.get("bit_depth") if essence == "video" else None)),
                # IDENT (incrustation 3 lignes) — slots vidéo uniquement.
                "ident":      (bool((slot_cfg or {}).get("ident")) if essence == "video" else None),
                "ident_size": (int((slot_cfg or {}).get("ident_size") or 0) if essence == "video" else None),
                # SDP actif (transport_file) : vidéo ET audio. La modale d'édition reste
                # vidéo-only côté UI, mais l'audio en a besoin pour afficher son multicast.
                "sdp": (((state_snapshot.get(rid, {}).get("active") or {})
                         .get("transport_file") or {}).get("data")),
                # Association audio→vidéo (groupement + nommage « Audio v-a ») — via rx_flows
                # (attached_to). None = flux INDÉPENDANT (non rattaché à une vidéo).
                "video_idx":     (_rx_vid_of.get((essence, idx)) if essence in ("audio", "anc") else None),
                "audio_sub_idx": (_rx_sub_of.get(("audio", idx)) if essence == "audio" else None),
                # Identité du flux composable (« Option A ») — pour add/remove granulaire côté UI.
                "flow_id":       _rx_id_of.get((essence, idx)),
                # Port (NIC) effectif du slot RX (multi-NIC) : {iface, pinned} ou None si mono-port.
                "port":          _slot_port(idx),
            })

        # Estimation théorique du débit RX (si pas de stats live du container)
        nic_rx_gbps = nic_info.get("rx_gbps")
        if nic_rx_gbps is None and n_video_cfg:
            try:
                from .. import plugins as _pl
                _p_defs = (_pl.get("2110_io") or {}).get("deploy_defaults") or {}
                w       = int(dc_params.get("width")  or _p_defs.get("width")  or 1920)
                h       = int(dc_params.get("height") or _p_defs.get("height") or 1080)
                fps_cfg = float(dc_params.get("fps")  or _p_defs.get("fps")    or 25)
                aper_cfg = int(dc_params.get("audio_per_video") or 0)
                n_active = min(int(dc_params.get("active_rx_count") or n_video_cfg), n_video_cfg)
                gbps_v = n_active * w * h * 20 * fps_cfg / 1e9 * 1.04
                gbps_a = n_active * aper_cfg * 48000 * 8 * 24 / 1e9
                nic_rx_estimated = round(gbps_v + gbps_a, 1)
            except Exception:
                nic_rx_estimated = None
        else:
            nic_rx_estimated = None
        # PLANIFIÉ = flux provisionnés (RX + TX) — chacun = 1 file AF-XDP quand il deviendra LIVE
        # (abonné/câblé). `xdp_active` (live) ≤ `xdp_planned` ≤ pool. Permet à la barre B2 de montrer
        # une zone « planifié » qui réagit aux ajouts/retraits de sources/destinations (en simu, un
        # flux ne consomme pas encore de file → seul le planifié bouge, l'actif suit à l'abonnement).
        from .. import io2110_flows as _iof
        _xdp_planned = len(_iof.active_flows(dc_params, "rx")) + len(_iof.active_flows(dc_params, "tx"))
        # Stats PAR PORT physique (multi-NIC) : fusion des descripteurs moteur (nic.ports : débit
        # mesuré / files / lien) + métadonnées nœud (nom de réseau → couleur red/blue UI) + comptage
        # de flux et CHARGE ESTIMÉE par port (repli quand ethtool ne mesure rien sur le port). Mono-
        # port → [] : l'UI retombe sur la tuile agrégée existante (pas de régression).
        nic_ports_out = []
        if _multi:
            _eng_ports = {p.get("iface"): p for p in (nic_info.get("ports") or [])}
            _est_per, _cnt_per = {}, {}
            _act = min(int(dc_params.get("active_rx_count") or n_video_cfg), n_video_cfg)
            for _rr in receivers_out:
                _pt = (_rr.get("port") or {}).get("iface")
                if not _pt:
                    continue
                _cnt_per[_pt] = _cnt_per.get(_pt, 0) + 1
                if _rr.get("essence") == "video" and int(_rr.get("idx") or 0) < _act:
                    _w = int(_rr.get("width") or 1920); _h = int(_rr.get("height") or 1080)
                    _f = float(dc_params.get("fps") or 25)
                    _est_per[_pt] = _est_per.get(_pt, 0.0) + _w * _h * 20 * _f / 1e9 * 1.04
            # Planifié PAR PORT : flux provisionnés (rx+tx) → port (assignation déterministe modulo/
            # épinglage). C'est un COMPTE DE FLUX exact par port (comme le planifié global) ; le LIVE
            # par port, lui, vient du moteur (sessions, fan-out compris) — d'où nic.ports[].active.
            _planned_per = {}
            _, _, _, _slot_tx = _io_media_ports(_node, dc_params, "tx_pins")
            for _f in _iof.active_flows(dc_params, "rx"):
                _p = (_slot_port(int(_f.get("idx") or 0)) or {}).get("iface")
                if _p: _planned_per[_p] = _planned_per.get(_p, 0) + 1
            for _f in _iof.active_flows(dc_params, "tx"):
                _p = (_slot_tx(int(_f.get("idx") or 0)) or {}).get("iface")
                if _p: _planned_per[_p] = _planned_per.get(_p, 0) + 1
            # État PTP par port (cache sampler, pas de SSH) : SLAVE|MASTER|PASSIVE|LISTENING|FAULTY…
            _ptp_states = {}
            try:
                from .. import ptp as _ptp
                _ptp_states = (_ptp.cached_status(c.get("node_id")) or {}).get("port_states") or {}
            except Exception:
                pass
            for _pm in _ports_meta:
                _if = _pm["ifname"]; _ep = _eng_ports.get(_if, {})
                _meas = _ep.get("rx_gbps")
                _rxq = _ep.get("rx_queues"); _txq = _ep.get("tx_queues")
                nic_ports_out.append({
                    "iface":               _if,
                    "alias":               _pm.get("alias"),
                    "network":             _pm.get("network"),
                    "primary":             _if in _auto_ports,
                    "rx_gbps":             _meas,
                    "tx_gbps":             _ep.get("tx_gbps"),
                    "rx_estimated_gbps":   (round(_est_per.get(_if, 0.0), 1) if _meas is None else None),
                    "port_capacity_gbps":  _ep.get("port_capacity_gbps") or nic_info.get("port_capacity_gbps") or 100,
                    "link_up":             _ep.get("link_up"),
                    "rx_queues":           _rxq,
                    "tx_queues":           _txq,
                    "rx_flow_count":       _cnt_per.get(_if, 0),
                    # Files AF-XDP PAR PORT (barre XDP par NIC) : live=sessions du moteur (exact),
                    # réservé=allocation mtl_init du port, planifié=flux provisionnés, plafond=HW du port.
                    "xdp_active":          _ep.get("active"),
                    "xdp_reserved":        ((_rxq or 0) + (_txq or 0)) or None,
                    "xdp_planned":         _planned_per.get(_if, 0),
                    # Plafond HW du port : per-port (moteur ≥0.34) sinon repli sur le global (même carte).
                    "xdp_hw":              _ep.get("hw_max_combined") or (data.get("xdp") or {}).get("hw_max_combined"),
                    "ptp_state":           _ptp_states.get(_if),
                    # Socle DPDK narrow (moteur ≥0.39.16, ports pmd=dpdk) : sessions RL TX live /
                    # cap RL du port (la limite dure — docs/chantiers/DPDK_NARROW.md §7) + sessions RX (RSS).
                    # None/absent sur un port af_xdp → l'UI garde la barre « Queues XDP ».
                    "pmd":                 _ep.get("pmd"),
                    "rl_tx_cap":           _ep.get("rl_tx_cap"),
                    "tx_sessions_active":  _ep.get("tx_sessions_active"),
                    "rx_sessions_active":  _ep.get("rx_sessions_active"),
                })
        # Modèle + agrégat NIC : on PRÉFÈRE les sources fiables de l'orchestrateur au modèle brut du
        # moteur (heuristique sysfs → « E810 QSFP » + 100G en dur, faux pour une 4-ports SFP).
        #  - modèle : nom exact résolu par lspci, persisté dans node_interfaces (config réseau).
        #  - agrégat : SOMME des vitesses de lien réelles des ports de la carte média (4×10 = 40G),
        #    via les ports du moteur (live) sinon les vitesses DB. Repli sur la valeur moteur.
        _db_model = None; _db_speeds = []
        if c.get("node_id"):
            from ..database import db_get_node_interfaces as _dgni
            for _r in (_dgni(c["node_id"]) or []):
                if _r.get("role") == "media2110":
                    if not _db_model and _r.get("model"):
                        _db_model = _r["model"]
                    if _r.get("speed_mbps"):
                        _db_speeds.append(int(_r["speed_mbps"]))
        # SMPTE 2022-7 « pair-aware » : une paire red/blue = UNE capacité UTILE (le second
        # port duplique le trafic, il n'ajoute rien). Capacité = min de la paire ; usage
        # utile = max de la paire (les deux legs portent les mêmes flux). Sans 2022-7 :
        # sommes classiques (aucune régression multi-NIC non redondant).
        from .. import docker_driver as _ddp
        _pairs227 = _ddp.media_port_pairs(_node) if _node else []
        _is227 = bool(dc_params.get("smpte_2022_7")) and bool(_pairs227)

        def _pair_sum(vals_by_if, reduce_fn):
            used, tot = set(), 0
            for _a, _b in _pairs227:
                pv = [v for v in (vals_by_if.get(_a), vals_by_if.get(_b)) if v is not None]
                if pv:
                    tot += reduce_fn(pv)
                used.update((_a, _b))
            for _k, _v in vals_by_if.items():
                if _k not in used and _v is not None:
                    tot += _v
            return tot

        _eng_ports_list = nic_info.get("ports") or []
        if _eng_ports_list:
            if _is227:
                _caps = {p.get("iface"): (p.get("port_capacity_gbps") or 0)
                         for p in _eng_ports_list}
                _agg = round(_pair_sum(_caps, min))
            else:
                _agg = round(sum((p.get("port_capacity_gbps") or 0) for p in _eng_ports_list))
        elif _db_speeds:
            _agg = round(sum(_db_speeds) / 1000)
            if _is227 and _pairs227 and len(_db_speeds) >= 2:
                _agg = round((sum(_db_speeds) - min(_db_speeds) * len(_pairs227)) / 1000)
        else:
            _agg = None
        nic_model_eff = _db_model or nic_info.get("model") or ""
        # Allège les préfixes génériques lspci (« Ethernet Network Adapter E810-XXV-4 » → « E810-XXV-4 »).
        for _pfx in ("Ethernet Network Adapter ", "Ethernet Controller ", "Ethernet Connection "):
            if nic_model_eff.startswith(_pfx):
                nic_model_eff = nic_model_eff[len(_pfx):]; break
        nic_agg_eff = _agg or nic_info.get("aggregate_gbps") or 100
        # Plafond HW XDP agrégé = SOMME des max combined par port (chaque PF E810 a son
        # budget propre) — pair-aware en 2022-7 (les files d'une paire portent les MÊMES
        # sessions). Repli mono-port (nic_ports_out vide) sur la valeur per-port du moteur.
        if _is227 and nic_ports_out:
            _hw_by_if = {p.get("iface"): p.get("xdp_hw") for p in nic_ports_out}
            _xdp_hw_total = _pair_sum(_hw_by_if, min) or (data.get("xdp") or {}).get("hw_max_combined")
        else:
            _hw_per_port = [p["xdp_hw"] for p in nic_ports_out if p.get("xdp_hw")]
            _xdp_hw_total = (sum(_hw_per_port) if _hw_per_port
                             else (data.get("xdp") or {}).get("hw_max_combined"))
        # Débits UTILES : en 2022-7 le trafic est dupliqué sur les deux legs → l'usage
        # d'une paire = max des deux ports (pas la somme).
        if _is227 and nic_ports_out:
            _rxg = {p.get("iface"): p.get("rx_gbps") for p in nic_ports_out}
            _txg = {p.get("iface"): p.get("tx_gbps") for p in nic_ports_out}
            if any(v is not None for v in _rxg.values()):
                nic_rx_gbps = round(_pair_sum(_rxg, max), 2)
            if any(v is not None for v in _txg.values()):
                nic_info = dict(nic_info)
                nic_info["tx_gbps"] = round(_pair_sum(_txg, max), 2)
        # ── Supervision RL (socle DPDK narrow) : bloc `rl` live du moteur (≥0.39.16) + repli
        # orchestrateur quand le moteur est muet (down / image antérieure) mais que le nœud est
        # en PF dpdk + pacing RL : cap RL par port via profil mesuré (nic_profiles) / bibliothèque
        # de cartes (_node_rl_tx_cap) — cf. _mtl_rl_tx_budget (même logique de repli).
        _rl_blk = data.get("rl") or {}
        _rl_active_eff = bool(_rl_blk.get("active"))
        _rl_cap_pp = _rl_blk.get("tx_cap_per_port")
        if not _rl_blk:
            try:
                from .. import docker_driver as _ddr
                if _node and _ddr._has_dpdk_pf(_node):
                    _pc, _ = _ddr._derive_pacing(_node)
                    if (_pc or "rl") == "rl":
                        _rl_active_eff = True
                        _rl_cap_pp = _ddr._node_rl_tx_cap(_node)
            except Exception:
                pass
        out.append({
            "vmid": vmid,
            "hostname": c.get("hostname") or f"#{vmid}",
            "ip": ip,
            "status": c.get("status"),
            "receivers": receivers_out,
            # Ports média du nœud (multi-NIC) : [] si mono-port → pas de sélecteur côté UI.
            "ports": _ports_meta,
            # SMPTE 2022-7 : paires red/blue actives → l'UI affiche/sélectionne des PAIRES.
            "smpte_2022_7": _is227,
            "port_pairs": [list(p) for p in _pairs227] if _is227 else [],
            # Stats ventilées par port physique (multi-NIC) : [] si mono-port.
            "nic_ports": nic_ports_out,
            # Capacité totale déployée vs slots visibles dans NMOS (pour le bouton « + »)
            "video_count":      n_video_cfg,
            "active_rx_count":  min(int(dc_params.get("active_rx_count") or n_video_cfg), n_video_cfg),
            # Stats NIC live (container up) ou estimées (container down)
            "nic_rx_gbps":            nic_rx_gbps,
            "nic_tx_gbps":            nic_info.get("tx_gbps"),
            "nic_rx_estimated_gbps":  nic_rx_estimated,
            "nic_port_capacity_gbps": nic_info.get("port_capacity_gbps") or 100,
            "nic_aggregate_gbps":     nic_agg_eff,
            "nic_model":              nic_model_eff,
            # Queues AF_XDP : sessions actives (live) / réservation mtl_init (plafond à chaud) / HW NIC.
            # `reserved` = files figées au dernier lancement (au-delà → redéploiement requis) ; `allocated`
            # suit la demande courante (legacy, conservé pour repli image pré-A2).
            "xdp_allocated":           (data.get("xdp") or {}).get("allocated"),
            "xdp_reserved":            (data.get("xdp") or {}).get("reserved"),
            "xdp_active":              (data.get("xdp") or {}).get("active"),
            "xdp_planned":             _xdp_planned,
            # Plafond HW : SOMME sur les 4 ports (chaque port E810 a son propre budget combined, ex.
            # 4×96=384) — `nic_ports_out[].xdp_hw` est le max PAR PORT. Le bloc `xdp` brut du moteur ne
            # lit que le port primaire (96), incomparable à `allocated`/`reserved` qui sont déjà des
            # sommes 4 ports → on agrège ici. Repli mono-port (nic_ports_out vide) : valeur moteur.
            "xdp_hw_max_combined":     _xdp_hw_total,
            "xdp_hw_current_combined": _xdp_hw_total,   # current==max sur E810 (tous canaux activés)
            "xdp_hw_xdp_available":    (None if _xdp_hw_total is None
                                        else max(0, _xdp_hw_total - ((data.get("xdp") or {}).get("allocated") or 0))),
            # Plafond combined PAR PORT (≠ agrégat ci-dessus) : utilisé par le calcul de budget de files
            # `_mtl_total_queues(...) × nb_ports`. Garder per-port pour ne pas double-compter.
            "xdp_hw_per_port":         (data.get("xdp") or {}).get("hw_max_combined"),
            # ── Socle DPDK narrow (bloc `rl` du moteur ≥0.39.16) : sous PF vfio le budget pertinent
            # n'est plus les files AF-XDP (hw_max_combined=None, pas de netdev kernel) mais les
            # SESSIONS RL par port en TX (cap RL_TX_QUEUES_CAP — docs/chantiers/DPDK_NARROW.md §7) et les files RSS
            # en RX. rl_tx_dropped = sessions TX au-delà du cap, IGNORÉES par le moteur → badge
            # SUR-CAPACITÉ côté UI. Cap par port : live du moteur, sinon repli orchestrateur
            # (profil mesuré / bibliothèque de cartes) quand le moteur est down ou pré-0.39.16.
            "rl_active":          _rl_active_eff,
            "rl_pacing":          _rl_blk.get("pacing"),
            "rl_tx_cap_per_port": _rl_cap_pp,
            "rl_tx_cap_total":    (_rl_cap_pp * _mtl_media_port_count(_node, dc_params)
                                   if (_rl_active_eff and _rl_cap_pp) else None),
            "rl_tx_sessions":     _rl_blk.get("tx_sessions"),
            "rl_rx_sessions":     _rl_blk.get("rx_sessions"),
            "rl_tx_dropped":      _rl_blk.get("tx_dropped"),
            "rl_rx_queues":       _rl_blk.get("rx_queues_alloc"),
            "rl_tx_queues":       _rl_blk.get("tx_queues_alloc"),
        })

    return out

@bp.route("/api/nmos/receivers_detail", methods=["GET"])
@require_login
def nmos_receivers_detail():
    """Tous les containers NMOS et leurs receivers (vue d'ensemble)."""
    return jsonify(_compute_receivers_detail())

@bp.route("/api/nmos/receivers/<int:vmid>/detail", methods=["GET"])
@require_login
def nmos_receiver_detail_one(vmid):
    """Détail NMOS d'un seul container — consommé par la carte plugin 2110_io
    montée dans le shell Sources. Renvoie {} si le container n'expose aucun receiver."""
    rows = _compute_receivers_detail(only_vmid=vmid)
    return jsonify(rows[0] if rows else {})


def _compute_senders_detail(only_vmid=None):
    """Agrège pour chaque container NMOS : ses senders (IS-04 IDs + state IS-05),
    + un fetch live des fps par pipeline depuis le /metrics du container.
    `only_vmid` → ne calcule que ce container (pour le control plugin par-instance)."""
    from services import nmos as _nmos
    from ..addressing import get_container_ip
    from ..metrics import tx_stalled_cache as _txst
    import requests as _req

    containers = db_get_containers()
    by_vmid = {c["vmid"]: c for c in containers}

    with _nmos._lock:
        send_snapshot = list(_nmos._senders.values())
        state_snapshot = {k: v for k, v in _nmos._send_state.items()}

    by_container = {}
    for s in send_snapshot:
        # tag vmid : peut être absent ou « None » (sender mal enregistré) → on ignore ce sender
        # au lieu de laisser int() lever (ValueError) et faire 500 sur TOUTE la page I/O.
        _vtag = (s.get("tags") or {}).get("urn:x-mxl:vmid") or [0]
        try:
            vmid = int(_vtag[0])
        except (ValueError, TypeError, IndexError):
            continue
        if only_vmid is not None and vmid != only_vmid:
            continue
        by_container.setdefault(vmid, []).append(s)

    import json as _json
    out = []
    for vmid, snds in sorted(by_container.items()):
        c = by_vmid.get(vmid, {})
        ip = c.get("ip") or get_container_ip(vmid)
        # deploy_config pour récupérer les shm_name côté input des senders
        dc = c.get("deploy_config")
        try:
            dc = _json.loads(dc) if isinstance(dc, str) else dc
        except Exception:
            dc = None
        dc_params = (dc or {}).get("params") or {}
        v_cfg = dc_params.get("video") or {}
        v_shm = v_cfg.get("shm_name") if dc_params else None
        a_shms = [a.get("shm_name") for a in (dc_params.get("audios") or [])]
        # Multi-NIC : ports média du nœud + assignation effective par slot TX (auto modulo / épinglé).
        from .. import docker_driver as _dd
        from ..database import db_get_node as _dgn
        _node = _dgn(c.get("node_id")) if c.get("node_id") else None
        _mifs = _dd._media_ifaces(_node) if _node else []
        _multi = len(_mifs) > 1
        if _multi:
            _prim = _dd._primary_network_id(_mifs, _node)
            _auto_ports = [e["ifname"] for e in _mifs if e.get("network_id") == _prim] \
                          or [e["ifname"] for e in _mifs]
            _declared = {e["ifname"] for e in _mifs}
            from ..database import db_get_media_networks as _dgmn
            _net_name = {n["id"]: n["name"] for n in _dgmn()}
            _ports_meta = [{"ifname": e["ifname"], "network": _net_name.get(e.get("network_id"))}
                           for e in _mifs]
            _tx_pins = dc_params.get("tx_pins") or {}
        else:
            _auto_ports, _declared, _ports_meta, _tx_pins = [], set(), [], {}
        def _slot_port(i, pins):
            if not _multi:
                return None
            p = pins.get(str(i))
            if p in _declared:
                return {"iface": p, "pinned": True}
            return {"iface": (_auto_ports[int(i) % len(_auto_ports)] if _auto_ports else None),
                    "pinned": False}

        per_key_fps = {}
        if ip:
            try:
                rr = _req.get(f"http://{ip}:8080", timeout=1.5)
                if rr.status_code == 200:
                    data = rr.json() or {}
                    for it in data.get("senders", []):
                        tx_i = it.get("tx_idx") if it.get("tx_idx") is not None else it.get("idx", 0)
                        key = (it.get("essence"), int(tx_i))
                        per_key_fps[key] = it
            except Exception:
                pass

        senders_out = []
        def _sort_key(s):
            st = state_snapshot.get(s["id"], {})
            return (0 if st.get("essence") == "video" else 1, st.get("audio_idx") or 0)
        for s in sorted(snds, key=_sort_key):
            sid = s["id"]
            st = state_snapshot.get(sid, {})
            essence = st.get("essence", "video")
            a_idx = st.get("audio_idx")
            tx_idx_st = st.get("tx_idx")
            if tx_idx_st is not None:
                live_key = (essence, int(tx_idx_st))
            else:
                live_key = (essence, 0 if essence == "video" else (a_idx or 0))
            live = per_key_fps.get(live_key, {})
            sub = s.get("subscription") or {}
            if essence == "video":
                shm_name = v_shm
            else:
                shm_name = a_shms[a_idx] if (a_idx is not None and 0 <= a_idx < len(a_shms)) else None
            # Format réel du slot TX : dc_params.tx_slots[tx_idx] porte les vraies dimensions (posées
            # au déploiement), alors que dc_params.video est un défaut global (souvent 1280x720). On
            # lit donc le slot d'abord, repli sur video puis défaut.
            _txsl = {}
            if essence == "video" and tx_idx_st is not None:
                _slots = dc_params.get("tx_slots") or []
                if 0 <= int(tx_idx_st) < len(_slots):
                    _txsl = _slots[int(tx_idx_st)] or {}
            senders_out.append({
                "id": sid,
                "label": s.get("label"),
                "essence": essence,
                "audio_idx": a_idx,
                "tx_idx": st.get("tx_idx"),
                "active": bool(sub.get("active")),
                "receiver_id": sub.get("receiver_id"),
                "multicast_ip": st.get("multicast_ip"),
                "destination_port": st.get("destination_port"),
                "shm_path": f"/dev/shm/{shm_name}" if shm_name else None,
                "fps": live.get("fps"),
                # Santé TX (moteur 2110_io) : cadence nominale + trames ayant raté leur epoch
                # (sous-cadence = scheduler saturé → « RTP alignment failure » au récepteur).
                "fps_nominal": live.get("fps_nominal"),
                "late": live.get("late"),
                # ⚠ `fps` N'EST PAS LA CADENCE DU FIL — ce commentaire l'affirmait, et c'est faux.
                # `mtl_rx.c:write_stats` le dit explicitement : le rejeu n'est PAS comptabilisé
                # (libmtl n'offre aucun signal fiable pour le compter), donc `fps` publie « les
                # trames NEUVES produites par le worker » et « SOUS-ESTIME la cadence du fil quand
                # la source est déficitaire ». La cadence réelle est garantie nominale par
                # l'horloge de sortie et ne se vérifie qu'au fil (compteurs de port, ou récepteur
                # tiers — cf. [[neuron-json-witness-endpoint]]).
                # Vécu le 2026-08-09 : `fps` à 37,8 sur un TX parfaitement alimenté ; le Neuron
                # verrouillait un 1080p50 propre avec ZÉRO erreur de séquence RTP au même instant.
                # Sur ce chemin, seuls `repeats` et `late` (compteurs d'ÉVÉNEMENTS) sont probants.
                # `fps_source` = fps − rejeu/s ; `repeats` = cumul. Absents (vieille image
                # moteur) → None, pas d'exception.
                "fps_source": live.get("fps_source"),
                "repeats": live.get("repeats"),
                # Famine TX : activé/câblé mais n'émet aucun flux (cf. metrics.tx_stalled_cache).
                "tx_stalled": (bool(_txst.get(vmid, {}).get(st.get("tx_idx"))) if essence == "video" else None),
                # Format vidéo (résolution + scan + chroma + profondeur) : slot TX réel > video > défaut.
                "width":  (int(_txsl.get("width")  or v_cfg.get("width")  or 1280) if essence == "video" else None),
                "height": (int(_txsl.get("height") or v_cfg.get("height") or 720) if essence == "video" else None),
                "scan":   ((_txsl.get("scan") or v_cfg.get("scan")) if essence == "video" else None),
                "chroma":    (v_cfg.get("chroma") if essence == "video" else None),
                "bit_depth": (v_cfg.get("bit_depth") if essence == "video" else None),
                # Port (NIC) effectif du slot TX (multi-NIC) : {iface, pinned} ou None si mono-port.
                "port": _slot_port(st.get("tx_idx") or 0, _tx_pins),
            })

        out.append({
            "vmid": vmid,
            "hostname": c.get("hostname") or f"#{vmid}",
            "ip": ip,
            "status": c.get("status"),
            "senders": senders_out,
            # Ports média du nœud (multi-NIC) : [] si mono-port → pas de sélecteur côté UI.
            "ports": _ports_meta,
        })

    return out

@bp.route("/api/nmos/senders_detail", methods=["GET"])
@require_login
def nmos_senders_detail():
    return jsonify(_compute_senders_detail())

@bp.route("/api/nmos/senders/<int:vmid>/detail", methods=["GET"])
@require_login
def nmos_sender_one_detail(vmid):
    """Détail NMOS d'UN container sender (par-vmid) — pour le control plugin monté."""
    rows = _compute_senders_detail(only_vmid=vmid)
    return jsonify(rows[0] if rows else {"vmid": vmid, "senders": []})


@bp.route("/api/io/mtl", methods=["GET"])
@require_login
def api_io_mtl():
    """Vue I/O 2110 des moteurs MTL (type 2110_io) : par moteur, les slots RX (abonnements)
    et TX (destinations). Alimente les onglets « Sources 2110 » / « Destinations 2110 » de /io.
    Structure prête video + audio + ANC + 2022-7 (leg2) — vidéo aujourd'hui, le reste vide."""
    import json as _json
    from ..database import db_get_containers, db_get_node
    from ..docker_driver import TX_SERVE_NEWEST_DEFAUT as _TX_SN_DEFAUT
    from services import nmos as _nmos
    engines = []
    for c in db_get_containers():
        try:
            dc = _json.loads(c.get("deploy_config") or "{}")
        except Exception:
            dc = {}
        if dc.get("type") != "2110_io":
            continue
        vmid = c["vmid"]; params = dc.get("params") or {}
        node = db_get_node(c.get("node_id")) or {}
        # Collecter tous les receivers (vidéo + audio + ANC) pour les associer par slot.
        rx_video = []; rx_audio_map = {}; rx_anc_map = {}
        for blk in _compute_receivers_detail(only_vmid=vmid):
            for r in blk.get("receivers", []):
                ess = r.get("essence")
                idx = r.get("idx", 0)
                if ess == "video":
                    rx_video.append(r)
                elif ess == "audio":
                    rx_audio_map.setdefault(idx, []).append(r)
                elif ess in ("anc", "data"):
                    rx_anc_map.setdefault(idx, []).append(r)
        n_vid = len(rx_video) or 1
        rx = []
        for r in rx_video:
            v_idx = r.get("idx", 0)
            audios = rx_audio_map.get(v_idx % n_vid, [])
            ancs   = rx_anc_map.get(v_idx % n_vid, [])
            rx.append({"slot": v_idx, "active": bool(r.get("active")),
                       "mcast": r.get("multicast_ip"), "port": r.get("destination_port"),
                       "source": r.get("sender_id"), "fps": r.get("fps"),
                       "shm": r.get("shm_path"),
                       "format": {"w": r.get("width"), "h": r.get("height"), "scan": r.get("scan")},
                       "audio": [{"slot": a.get("idx"), "active": bool(a.get("active")),
                                  "mcast": a.get("multicast_ip"), "port": a.get("destination_port")}
                                 for a in audios],
                       "anc":   [{"slot": a.get("idx"), "active": bool(a.get("active")),
                                  "mcast": a.get("multicast_ip"), "port": a.get("destination_port")}
                                 for a in ancs],
                       "leg2": None})
        # fps live des senders TX, indexés par id NMOS
        live_by_id = {}
        for blk in _compute_senders_detail(only_vmid=vmid):
            for s in blk.get("senders", []):
                live_by_id[s["id"]] = s
        def _ess(t, pfx):   # destination d'une essence : leg0 + leg1 (2022-7)
            return {"mcast": t.get(pfx + "multicast_ip"), "port": t.get(pfx + "dest_port"),
                    "mcast2": t.get(pfx + "multicast_ip_leg1"), "port2": t.get(pfx + "dest_port_leg1")}
        tx_slots_full = params.get("tx_slots") or []
        # PRÉSERVER un 0 explicite (aucun TX actif) : `or 6` traiterait 0 comme « non défini » → 6
        # senders fantômes. On ne retombe sur 6 que si la clé est absente/None.
        _atc = params.get("active_tx_count")
        active_tx_count = min(6 if _atc is None else int(_atc), len(tx_slots_full))
        _n_audio_full = int(params.get("audio_count") or 0)
        _n_tx_full    = max(1, int(params.get("tx_count") or 0) or len(tx_slots_full))
        _max_aud_per_tx = max(1, _n_audio_full // _n_tx_full) if _n_audio_full > 0 else 1
        # « Option A » : audios/ANC par slot pilotés par tx_flows (attached_to), N quelconque. Map
        # (essence, idx)→flow_id pour l'add/remove granulaire côté UI Destinations.
        from .. import io2110_flows as _iof
        _tx_flows = _iof.active_flows(params, "tx")
        _tx_id_of = {(f["essence"], f["idx"]): f["id"] for f in _tx_flows}
        # C2b+ : ressource NMOS bindée par slot/essence (rebinding explicite) — {id,label} ou None.
        _nb = params.get("nmos_bind") or {}
        from ..database import db_nmos_resource_get as _nres
        def _blbl(rid):
            if not rid:
                return None
            r = _nres(rid)
            return {"id": rid, "label": (r or {}).get("label")}
        # Port (NIC) effectif par slot TX (multi-NIC) : auto modulo / épinglé via tx_pins.
        _multi_tx, _pm_tx, _ap_tx, _slot_tx = _io_media_ports(node, params, "tx_pins")
        tx = []
        for i, t in enumerate(tx_slots_full[:active_tx_count]):
            sid = (_nmos.sender_sid_for(vmid, tx_idx=i, essence="video")
                   or _nmos._stable_uuid("sender:v:{}:tx{}".format(vmid, i)))
            # SDP PAR ESSENCE. NMOS publie un sender distinct par essence (vidéo, chaque audio,
            # ANC), chacun avec son propre fichier de transport. La page ne servait qu'un seul
            # « SDP » par slot, construit sur le sender VIDÉO : ouvert sur un slot vidéo+audio, il
            # montrait un SDP sans audio dedans — de quoi conclure que l'audio n'est pas annoncé
            # alors qu'il l'est, sous un autre sender. Chaque essence porte donc désormais le sien.
            def _sdp(sid_):
                return ("/x-nmos/connection/{}/single/senders/{}/transportfile"
                        .format(_nmos.IS05_VERSION, sid_)) if sid_ else None
            # Format AUDIO d'une sortie 2110-30. Ce n'est PAS une mesure — c'est ce que le moteur
            # crée, en dur, dans mtl_rx.c : `ops.fmt = ST30_FMT_PCM24` (L24) et
            # `ops.sampling = ST30_SAMPLING_48K`. Le nombre de canaux vient de sa config avec 8 en
            # défaut (`jint(j,"channels",8)`) et l'orchestrateur ne l'envoie jamais → 8 aujourd'hui.
            # On le calcule ICI, à un seul endroit, plutôt que d'écrire « 48kHz / L24 / 8ch » dans
            # l'interface : le jour où le nombre de canaux devient réglable, la page suivra au lieu
            # de mentir. (Le ptime, lui, est déjà PAR SORTIE — il voyage avec chaque flux.)
            _a_fmt = {"sample_rate": 48000, "bit_depth": 24,
                      "channels": int(params.get("tx_audio_channels") or 8)}
            # Chemin MXL D'ENTRÉE par essence : c'est le pendant exact du chemin de sortie affiché
            # par source en réception. La vidéo l'exposait déjà (shm_in) ; les audios et l'ANC ont
            # le leur depuis toujours (`tx_audio{i}_shm` / `tx_anc{i}_shm`, câblés par
            # docker_driver) sans qu'aucune page ne le montre — donc impossible de vérifier d'un
            # coup d'œil qu'une sortie audio lit bien le bus qu'on croit.
            vid = _ess(t, ""); vid["width"] = t.get("width"); vid["height"] = t.get("height")
            vid["sdp_href"] = _sdp(sid)
            vid["shm_in"] = params.get(cle_tx_shm(i)) or ""
            _anc = _ess(t, "anc_")
            _anc["sdp_href"] = _sdp(_nmos.sender_sid_for(vmid, tx_idx=i, essence="anc")
                                    or _nmos._stable_uuid("sender:d:{}:tx{}".format(vmid, i)))
            _anc["shm_in"] = params.get(cle_tx_anc_shm(i)) or ""
            _aud_idxs = _iof.tx_slot_audio_idxs(_tx_flows, i)
            def _aud(ai, _t=t, _aidxs=_aud_idxs, _i=i):
                a = (_t.get("audios") or [])[ai] if ai < len(_t.get("audios") or []) else {}
                _sida = (_nmos.sender_sid_for(vmid, tx_idx=_i, essence="audio", audio_idx=ai)
                         or _nmos._stable_uuid("sender:a:{}:tx{}:{}".format(vmid, _i, ai)))
                _aidx = _aidxs[ai] if ai < len(_aidxs) else None
                return {"mcast": a.get("multicast_ip"), "port": a.get("dest_port"),
                        "mcast2": a.get("multicast_ip_leg1"), "port2": a.get("dest_port_leg1"),
                        "tone": a.get("tone"),   # config générateur de tonalité (UI éditeur TONE)
                        "sdp_href": _sdp(_sida),
                        "shm_in": (params.get(cle_tx_audio_shm(_aidx)) or "") if _aidx is not None else "",
                        # ptime PAR SORTIE (ms) : la seule part du format audio qui varie
                        # réellement d'un flux à l'autre.
                        "ptime": a.get("ptime"),
                        "sample_rate": _a_fmt["sample_rate"], "bit_depth": _a_fmt["bit_depth"],
                        "channels": _a_fmt["channels"],
                        "flow_id": _tx_id_of.get(("audio", _aidx)) if _aidx is not None else None}
            tx.append({"slot": i,
                       "shm_in": params.get(cle_tx_shm(i)) or "",
                       "fps": (live_by_id.get(sid) or {}).get("fps"),
                       # Santé TX : sous-cadence (fps < nominal) ou late > 0 = epochs ratés
                       # (scheduler saturé) → badge ⚠ côté UI Destinations.
                       "fps_nominal": (live_by_id.get(sid) or {}).get("fps_nominal"),
                       "late": (live_by_id.get(sid) or {}).get("late"),
                       # ⚠ `fps` n'est PAS la cadence du fil (cf. le bloc détaillé plus haut dans
                       # ce fichier) : c'est le nombre de trames NEUVES prises par le worker, et il
                       # sous-estime le fil quand la source est déficitaire. Le fil, lui, est tenu
                       # par l'horloge de sortie. `fps_source` = fps − rejeu/s, `repeats` = cumul.
                       "fps_source": (live_by_id.get(sid) or {}).get("fps_source"),
                       "repeats": (live_by_id.get(sid) or {}).get("repeats"),
                       "tx_stalled": (live_by_id.get(sid) or {}).get("tx_stalled"),
                       "sender_id": sid,
                       # flow_id (« Option A ») : vidéo du slot + ANC, pour l'add/remove granulaire UI.
                       "flow_id": _tx_id_of.get(("video", i)),
                       "anc_flow_id": _tx_id_of.get(("anc", i)),
                       # Conservé pour compatibilité : c'est le SDP de la VIDÉO du slot. Les
                       # consommateurs doivent préférer `video.sdp_href` / `audios[].sdp_href` /
                       # `anc.sdp_href`, qui disent de quelle essence ils parlent.
                       "sdp_href": _sdp(sid),
                       "video": vid, "audios": [_aud(ai) for ai in range(len(_aud_idxs))],
                       "anc": _anc,
                       "bind": {"video": _blbl(_nb.get(slot_tx(i, "v"))),
                                "audios": [_blbl(_nb.get(slot_tx(i, "a%d" % ai)))
                                           for ai in range(len(_aud_idxs))],
                                "anc": _blbl(_nb.get(slot_tx(i, "d")))},
                       "gen": bool(t.get("gen_enabled")),
                       "gen_pattern": t.get("gen_pattern") or "bars",
                       "ident": bool(t.get("ident")),
                       "ident_size": int(t.get("ident_size") or 0),
                       # Rythme d'émission (mode tranche) : 0 = attendre l'image suivante (défaut),
                       # >0 = grille d'émission décalée de N µs (TROFF au SDP) → sélecteur UI.
                       "epoch_shift_us": int(t.get("epoch_shift_us") or 0),
                       # Choix de la trame émise (mode tranche) : 1 = la plus RÉCEMMENT prête
                       # (~1 image de latence en moins), 0 = la plus ancienne (historique).
                       # ⚠ Le défaut ne se REDÉRIVE PAS ici : on LIT celui de `docker_driver`, qui
                       # est ce que le moteur reçoit réellement. Les deux valeurs étaient calculées
                       # séparément avec la même règle écrite deux fois — donc deux occasions de
                       # diverger, et un affichage qui aurait menti sur l'état réel de la sortie.
                       "serve_newest": (_TX_SN_DEFAUT if t.get("serve_newest") is None
                                        else (1 if t.get("serve_newest") else 0)),
                       # Format GÉN configuré du slot (repli sur le format moteur) → présélection UI.
                       # fps_cfg ≠ fps (live) ci-dessus.
                       "width":   int(t.get("width")  or params.get("width")  or 1920),
                       "height":  int(t.get("height") or params.get("height") or 1080),
                       "fps_cfg": float(t.get("fps")  or params.get("fps")    or 25),
                       "scan":    str(t.get("scan")   or params.get("scan")   or "p"),
                       # PROFONDEUR EFFECTIVE de la sortie : celle du SLOT si elle y est posée,
                       # sinon celle du moteur. On lisait le moteur seul — donc une sortie réglée
                       # en 10 bits s'affichait avec les 8 bits du moteur, et le réglage qu'on
                       # venait d'appliquer semblait n'avoir rien fait.
                       # Le CHROMA reste un réglage moteur (l'endpoint de format ne le prend pas).
                       # La COLORIMÉTRIE n'est volontairement pas remontée : le moteur ne la
                       # connaît pas (script.py n'a que chroma/bit_depth), et afficher un « 709 »
                       # que rien ne pose ferait passer une supposition pour une déclaration.
                       "chroma":    params.get("chroma"),
                       "bit_depth": t.get("bit_depth") or params.get("bit_depth"),
                       # Port (NIC) effectif du slot TX (multi-NIC) : {iface, pinned} ou None.
                       "port":    _slot_tx(i)})
        # Stats NIC pour l'onglet Destinations (TX)
        _rx_blk = next((_b for _b in _compute_receivers_detail(only_vmid=vmid)), {})
        # Ventilation par port physique : on réutilise les descripteurs moteur calculés côté RX
        # (débit mesuré / files / lien, role-agnostique) et on y ajoute le compte de flux TX par
        # port (le rx_flow_count vient déjà du bloc RX). Mono-port → [] (UI = tuile agrégée).
        _nic_ports = _rx_blk.get("nic_ports") or []
        if _multi_tx and _nic_ports:
            _txc = {}
            for _ts in tx:
                _p = (_ts.get("port") or {}).get("iface")
                if _p:
                    _txc[_p] = _txc.get(_p, 0) + 1
            for _np in _nic_ports:
                _np["tx_flow_count"] = _txc.get(_np["iface"], 0)
        # Plafond TX activable = budget de queues XDP partagé (live si le contrôleur l'expose,
        # sinon réglage). On reporte ce plafond comme `tx_count` → le bouton « + Ajouter un TX »
        # n'apparaît que tant qu'il reste du budget de queues (et ne pousse jamais vers l'ENOMEM).
        _live_total = _rx_blk.get("xdp_hw_per_port")   # PAR PORT (≠ agrégat affiché) — × nb de ports ci-dessous
        # Budget AGRÉGÉ : chaque port média a son propre budget de files → × nb de ports (round-robin).
        _nports = _mtl_media_port_count(node, params)
        _total_q = _mtl_total_queues({"hw_max_combined": _live_total} if _live_total else None) * _nports
        # Socle DPDK narrow : sous pacing RL le plafond TX = budget de sessions RL (cap RL/port ×
        # ports — la limite dure constatée au banc, docs/chantiers/DPDK_NARROW.md §7), PAS les files AF-XDP (le
        # repli réglage 48 sur-plafonnait ou sous-plafonnait selon la carte). None sur un nœud
        # af_xdp → budget partagé historique inchangé.
        _cap_rx, _cap_tx = _mtl_active_caps(params, _total_q, node=node,
                                            tx_budget=_mtl_rl_tx_budget(_rx_blk, params, node))
        engines.append({"vmid": vmid, "hostname": c.get("hostname") or "#{}".format(vmid),
                        "node": node.get("name") or node.get("host") or "",
                        "fallback": params.get("tx_fallback") or "black",
                        "active_rx_count": (lambda v: 6 if v is None else int(v))(params.get("active_rx_count")),
                        "video_count":     int(params.get("video_count") or 0),
                        "active_tx_count": active_tx_count,
                        "tx_count":        _cap_tx,
                        "tx_provisioned":  len(tx_slots_full),
                        "rx": rx, "tx": tx,
                        # Ports média du nœud + stats ventilées par port (multi-NIC) : [] si mono-port.
                        "ports":      _rx_blk.get("ports") or [],
                        "smpte_2022_7": _rx_blk.get("smpte_2022_7") or False,
                        "port_pairs":   _rx_blk.get("port_pairs") or [],
                        "nic_ports":  _nic_ports,
                        "nic_rx_gbps":           _rx_blk.get("nic_rx_gbps"),
                        "nic_tx_gbps":           _rx_blk.get("nic_tx_gbps"),
                        "nic_rx_estimated_gbps": _rx_blk.get("nic_rx_estimated_gbps"),
                        "nic_port_capacity_gbps": _rx_blk.get("nic_port_capacity_gbps") or 100,
                        "nic_aggregate_gbps":    _rx_blk.get("nic_aggregate_gbps") or 100,
                        "nic_model":             _rx_blk.get("nic_model") or "",
                        # Queues AF_XDP (sessions actives / budget) — passthrough pour le compteur
                        # ⬡ du header Destinations (io2110.js). Null tant que l'image n'expose pas xdp.
                        "xdp_allocated":           _rx_blk.get("xdp_allocated"),
                        "xdp_reserved":            _rx_blk.get("xdp_reserved"),
                        "xdp_active":              _rx_blk.get("xdp_active"),
                        "xdp_planned":             _rx_blk.get("xdp_planned"),
                        "xdp_hw_max_combined":     _rx_blk.get("xdp_hw_max_combined"),
                        "xdp_hw_current_combined": _rx_blk.get("xdp_hw_current_combined"),
                        "xdp_hw_xdp_available":    _rx_blk.get("xdp_hw_xdp_available"),
                        # Supervision RL (socle DPDK narrow) — passthrough du bloc RX (cf.
                        # _compute_receivers_detail) : l'UI Destinations remplace la barre
                        # « Queues XDP » par « Sessions TX (RL) » quand rl_active est vrai.
                        "rl_active":          _rx_blk.get("rl_active"),
                        "rl_tx_cap_per_port": _rx_blk.get("rl_tx_cap_per_port"),
                        "rl_tx_cap_total":    _rx_blk.get("rl_tx_cap_total"),
                        "rl_tx_sessions":     _rx_blk.get("rl_tx_sessions"),
                        "rl_rx_sessions":     _rx_blk.get("rl_rx_sessions"),
                        "rl_tx_dropped":      _rx_blk.get("rl_tx_dropped"),
                        "rl_rx_queues":       _rx_blk.get("rl_rx_queues")})
    return jsonify({"engines": engines})


@bp.route("/api/nmos/receivers/<int:vmid>/<int:idx>/sdp", methods=["POST"])
@require_perm("containers.deploy")
def nmos_receiver_sdp(vmid, idx):
    """Abonnement manuel d'un flux receiver à partir d'un SDP collé.
    Body: {sdp: str, enabled: bool (défaut true), essence: 'video'}.
    Réutilise la chaîne IS-05 (staged → activate_immediate → agent)."""
    from services import nmos as _nmos
    data    = request.json or {}
    enable  = data.get("enabled", True)
    essence = data.get("essence") or "video"
    sdp     = (data.get("sdp") or "").strip()
    if enable and not sdp:
        return jsonify({"ok": False, "error": "SDP vide"}), 400
    code, res = _nmos.manual_subscribe(vmid, idx, essence, sdp, enable=enable)
    if code != 200:
        return jsonify({"ok": False, "error": res.get("error", "échec")}), code
    return jsonify({"ok": True})


@bp.route("/api/nmos/receivers/<int:vmid>/<int:idx>/activate_sender", methods=["POST"])
@require_perm("containers.deploy")
def nmos_receiver_activate_sender(vmid, idx):
    """Déclenchement manuel de l'activation IS-05 du sender NMOS distant (bouton, cf.
    services.nmos.manual_activate_remote_sender) — pour le matériel dont l'activation
    automatique échoue (mDNS/découverte peu fiables). Body optionnel: {essence: 'video'}."""
    from services import nmos as _nmos
    essence = (request.json or {}).get("essence") or "video"
    res = _nmos.manual_activate_remote_sender(vmid, idx, essence)
    return jsonify(res), (200 if res.get("ok") else 409)
