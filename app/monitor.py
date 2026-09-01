# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Per-user WebRTC monitoring encoder.

Each connected user gets a dedicated `worker_udp` container (hostname
`monitor-u<uid>`) that encodes an arbitrary /dev/shm signal and pushes it to the
MediaMTX gateway on a fixed per-user WebRTC path (`monitor-u<uid>`). The global
side panel embeds that path's WHEP player. Clicking "Monitoring" on a producer
page re-points the user's encoder to that producer's output shm.

Lifecycle:
- created on demand (user-triggered, streamed progress) — requires the WebRTC
  gateway to be deployed + enabled;
- the script is stopped after IDLE_TIMEOUT without activity (heartbeat / source
  change) by a background reaper, and restarted on reopen. The container is kept.

New-code naming in English by request (CLAUDE.md French convention overridden)."""
import json
import logging
import os
import sys
import threading
import time

import requests

from . import settings as st
from . import plugins as _plugins
from . import deploy
from .database import (db_get_containers, db_get_container, db_get_user_by_id,
                       db_set_monitor_user)
from .containers import detruire_container
from .deploy import deployer_script

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 600     # 10 min sans activité → stop du script
REAP_INTERVAL = 60

_last_used = {}        # uid → timestamp de dernière activité
_source = {}           # uid → {"shm": ..., "label": ...}
_create_lock = threading.Lock()   # sérialise check+create des containers monitor
                                  # (deux requêtes concurrentes = deux containers orphelins sinon)


def _sanitize_host(s):
    """Fragment de hostname valide : ASCII, [A-Za-z0-9-]. Délègue à la source unique
    (`app/hostnames.py`) — c'était une copie littérale de l'algorithme, en trois exemplaires."""
    from .hostnames import normaliser
    return normaliser(s)


def _hostname(uid):
    """Hostname du monitor : « Monitoring-<Initiale prénom><Nom> » (ex. Monitoring-ABernard).
    Repli sur le username puis sur l'uid si prénom/nom absents. Sans espace (Proxmox)."""
    try:
        u = db_get_user_by_id(int(uid)) or {}
    except Exception:
        u = {}
    prenom = (u.get("prenom") or "").strip()
    nom = (u.get("nom") or "").strip()
    suffix = ""
    if nom:
        suffix = (prenom[:1] + nom) if prenom else nom
    elif u.get("username"):
        suffix = u["username"]
    suffix = _sanitize_host(suffix)
    if not suffix:
        return f"Monitoring-u{int(uid)}"
    return _sanitize_host(f"Monitoring-{suffix}")[:63]


def _path(uid):
    # Path WebRTC : technique et STABLE (URL-safe), indépendant du hostname affiché.
    return f"monitor-u{int(uid)}"


def _create_monitor_container(hostname, deploy_type="streamer"):
    """Crée un container monitor Docker compute. Lève RuntimeError si aucun nœud disponible."""
    from .docker_compute import pick_compute_node, creer_container_compute
    node_id = pick_compute_node(deploy_type=deploy_type)
    if not node_id:
        raise RuntimeError("Aucun nœud compute disponible (Réglages → Nœuds).")
    return creer_container_compute(node_id, deploy_type, hostname=hostname)


def gateway_ready():
    return bool(st.get("webrtc_enabled") and st.get("webrtc_gateway_ip"))


def _embed_url(uid):
    ip = st.get("webrtc_gateway_ip")
    if not ip:
        return None
    return f"http://{ip}:{int(st.get('webrtc_http_port') or 8889)}/{_path(uid)}"


def _container_for(uid):
    """Container monitor de l'utilisateur. Match d'abord par monitor_user_id (stable,
    robuste au renommage), sinon repli sur l'ancien hostname monitor-u<uid> (compat)."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    legacy = f"monitor-u{uid}"
    containers = db_get_containers()
    for c in containers:
        if c.get("monitor_user_id") == uid:
            return c
    for c in containers:
        if c.get("hostname") == legacy:
            return c
    return None


def _params(uid, shm="", audio_shm=None, fmt=None, hot=False):
    """Params du monitor : encode `shm` (vidéo) + `audio_shm` optionnel en H.264/Opus et pousse
    un leg WebRTC sur le path per-user. Le format d'ENTRÉE (résolution, chroma, profondeur,
    colorimétrie) SUIT la source (`fmt` issu de `_shm_fmt`) — aucune valeur statique : un
    consommateur doit lire le shm avec le format réel du producteur, pas des défauts.

    - hot=True : hot-input (source re-câblable via :8082 sans redéployer), dims FIXES (fmt requis).
    - sinon : fmt absent → width/height=0 → auto-détection (dernier recours, résolution inconnue)."""
    f = fmt or {}
    chroma = f.get("chroma") or "422"
    return {
        "shm_name": shm or "",
        "audio_shm": audio_shm or None,
        "hot_input": bool(hot),
        # Format d'ENTRÉE lu dans le shm (top-level CONFIG.chroma/bit_depth côté streamer).
        "chroma": chroma,
        "bit_depth": int(f.get("bit_depth") or 8),
        "scan": f.get("scan") or "p",
        "field_order": f.get("field_order") or "",
        # `encoder: auto` — le monitor est le cas d'usage le plus favorable à l'encodage matériel :
        # un encodeur par utilisateur connecté, jetable, à la qualité peu critique, et ~1 cœur
        # économisé par utilisateur (mesuré). En `auto` il prend la carte si le nœud en a une de
        # libre et retombe sur x264 sinon, sans jamais empêcher la création du monitor — un
        # monitoring qui refuse de démarrer faute de GPU serait une régression franche.
        "video": {"codec": "h264", "bitrate": "2M", "preset": "ultrafast",
                  "encoder": "auto", "nvenc_preset": "p1", "nvenc_tune": "ull",
                  "gop": 25, "width": int(f.get("w") or 0), "height": int(f.get("h") or 0),
                  "fps": 25, "chroma": chroma, "colorimetry": f.get("colorimetry") or ""},
        "audio": {"enabled": bool(audio_shm), "bitrate": "128k",
                  "tracks": [{"channels": [0, 1]}] if audio_shm else []},
        "destinations": [{"type": "webrtc", "path": _path(uid), "enabled": True}],
    }


def _shm_fmt(shm):
    """Format COMPLET d'un shm vidéo, lu depuis le deploy_config de son producteur en DB :
    {w, h, chroma, bit_depth, scan, colorimetry, fps}. None si introuvable (→ auto-détection).
    Le moniteur (et tout consommateur) doit SUIVRE le format réel de la source, pas des défauts.
    `fps` : cadence DÉCLARÉE du producteur, telle quelle (25, 29.97 ou "30000/1001") — ""
    si inconnue. Consommée par le chip format du multiview (normalisée par _rate_nd côté script)."""
    import json
    shm = (shm or "").strip()
    if not shm:
        return None
    for c in db_get_containers():
        dc_raw = c.get("deploy_config")
        try:
            dc = json.loads(dc_raw) if isinstance(dc_raw, str) else dc_raw
        except Exception:
            dc = None
        if not dc:
            continue
        t = dc.get("type"); p = dc.get("params") or {}
        hn = c.get("hostname") or f"mxl{c['vmid']}"
        if not _plugins.is_plugin(t):
            continue
        # out_width/out_height (multiview) ont priorité, sinon width/height (format).
        # Multiview portrait : le flux émis est tourné 90° → on expose les dims SWAPPÉES (cf
        # scripts.multiview_output_dims), pas le canevas de design vertical.
        _ow = int(p.get("out_width") or p.get("width") or 1280)
        _oh = int(p.get("out_height") or p.get("height") or 720)
        if str(p.get("orientation") or "").strip().lower() in ("portrait_cw", "portrait_ccw"):
            _ow, _oh = _oh, _ow
        fmt = {
            "w": _ow,
            "h": _oh,
            "chroma":      str(p.get("chroma") or "422"),
            "bit_depth":   int(p.get("bit_depth") or 8),
            "scan":        str(p.get("scan") or "p"),
            "field_order": str(p.get("field_order") or ""),
            "colorimetry": str(p.get("colorimetry") or ""),
            "fps":         str(p.get("fps") or ""),
        }
        phn = p.get("hostname") or hn
        # Surcharge PAR-FLUX (2110_io) : scan/dims RÉELS de l'entrée idx, posés par-flux dans
        # `rx_fmt[idx]` à l'abonnement (services/nmos:_propagate_sdp_format, lu du SDP). Sans ça,
        # le scan GLOBAL « dernier flux activé » écrasait les entrées entrelacées en progressif.
        _rxs = (p.get("rx_fmt") or {}) if t == "2110_io" else {}
        def _ovr(d):
            if d and _rxs:
                _pre = phn + "_"
                if shm.startswith(_pre) and shm[len(_pre):].isdigit():
                    _sf = _rxs.get(shm[len(_pre):])
                    if _sf:
                        if _sf.get("width"):  d["w"] = int(_sf["width"])
                        if _sf.get("height"): d["h"] = int(_sf["height"])
                        if _sf.get("scan"):   d["scan"] = str(_sf["scan"])
                        if _sf.get("field_order") is not None:
                            d["field_order"] = str(_sf["field_order"])
                        if _sf.get("fps"):    d["fps"] = str(_sf["fps"])
            return d
        for prod in _plugins.derive_wiring(t, phn, p)["produces"]:
            if prod.get("shm") == shm and (prod.get("essence") or "video") == "video":
                # Priorité 1 : format déclaré dans produces[] (ex. mixer, multiview…)
                pf = prod.get("format") or {}
                if pf.get("width") and pf.get("height"):
                    return _ovr({
                        "w":           int(pf["width"]),
                        "h":           int(pf["height"]),
                        "chroma":      str(pf.get("chroma") or fmt["chroma"]),
                        "bit_depth":   int(pf.get("bit_depth") or fmt["bit_depth"]),
                        "scan":        str(pf.get("scan") or fmt["scan"]),
                        "field_order": str(pf.get("field_order") or fmt["field_order"]),
                        "colorimetry": str(pf.get("colorimetry") or fmt["colorimetry"]),
                        "fps":         str(pf.get("fps") or fmt["fps"]),
                    })
                # Priorité 2 : params top-level connus (mixer/UDC/color_corrector)
                if fmt["w"] and fmt["h"]:
                    return _ovr(fmt)
                # Priorité 3 : métriques live du container producteur (2110_io —
                # résolution négociée au SDP, absente du wiring statique).
                ip = c.get("ip")
                if ip:
                    try:
                        import requests as _req
                        m = _req.get(f"http://{ip}:8080/", timeout=1.5).json()
                        for rx in (m.get("receivers") or []):
                            if (rx.get("shm_out") == shm
                                    and rx.get("width") and rx.get("height")):
                                return _ovr({
                                    "w":         int(rx["width"]),
                                    "h":         int(rx["height"]),
                                    "chroma":    str(rx.get("chroma") or fmt["chroma"]),
                                    "bit_depth": int(rx.get("bit_depth") or fmt["bit_depth"]),
                                    "scan":      fmt["scan"],
                                    "field_order": fmt["field_order"],
                                    "colorimetry": fmt["colorimetry"],
                                    "fps":       str(rx.get("fps") or fmt["fps"]),
                                })
                    except Exception:
                        pass
                return _ovr(None)   # format inconnu → auto-détect côté script
    return None


def _shm_dims(shm):
    """Compat : (w,h) du shm vidéo, ou None. Dérivé de `_shm_fmt` (format complet)."""
    f = _shm_fmt(shm)
    return (f["w"], f["h"]) if f else None


def _fmt_from_flow_def(d):
    """{w,h,chroma,bit_depth,scan,field_order,fps,colorimetry} depuis un flowDef MXL (dict).
    Chroma déduit du sous-échantillonnage Cb vs Y. None si pas de dimensions exploitables."""
    comps = {c.get("name"): c for c in (d.get("components") or [])}
    y = comps.get("Y") or {}
    cb = comps.get("Cb") or {}
    # Dims de TRAME d'abord (`frame_width`/`frame_height` : seuls champs qui font foi en
    # entrelacé — un producteur tiers peut déclarer ses composants à la hauteur de CHAMP).
    w = int(d.get("frame_width") or y.get("width") or 0)
    h = int(d.get("frame_height") or y.get("height") or 0)
    if not (w and h):
        return None
    cw = int(cb.get("width") or w)
    ch = int(cb.get("height") or h)
    chroma = "444" if (cw == w and ch == h) else "420" if (cw * 2 == w and ch * 2 == h) else "422"
    im = str(d.get("interlace_mode") or "progressive")
    fo = "tff" if "tff" in im else ("bff" if "bff" in im else "")
    gr = d.get("grain_rate") or {}
    num, den = gr.get("numerator"), gr.get("denominator") or 1
    fps = (str(num) if den == 1 else "%s/%s" % (num, den)) if num else ""
    return {"w": w, "h": h, "chroma": chroma, "bit_depth": int(y.get("bit_depth") or 8),
            "scan": "p" if im.startswith("progressive") else "i", "field_order": fo,
            "colorimetry": "", "fps": fps}


def _shm_fmt_node(container, shm):
    """Repli quand `_shm_fmt` échoue (flux ORPHELIN : répliqué RDMA, ou créé hors orchestrateur) :
    lit le flowDef MXL RÉEL sur le nœud du conteneur consommateur (/dev/shm/mxl/<uuid>.mxl-flow/
    flow_def.json) → format correct, donc encodage correct. None si introuvable."""
    try:
        from .database import db_get_node
        node = db_get_node(container.get("node_id")) if container.get("node_id") else None
        if not node:
            return None
        st = os.path.join(os.path.dirname(os.path.dirname(__file__)), "script_templates")
        if st not in sys.path:
            sys.path.insert(0, st)
        import bobimxl
        from . import node_driver
        uuid = bobimxl.flow_id(shm)
        rc, out, _ = node_driver.host_exec(
            node, "cat /dev/shm/mxl/%s.mxl-flow/flow_def.json 2>/dev/null" % uuid, timeout=8)
        s = (out or "").strip()
        return _fmt_from_flow_def(json.loads(s)) if s.startswith("{") else None
    except Exception:
        return None


def touch(uid):
    _last_used[uid] = time.time()


def _script_running(ip, vmid=None):
    if not ip:
        return False
    try:
        r = deploy.agent_session().get(deploy.agent_url(ip, "/status"), timeout=2,
                                       headers=deploy.agent_headers(vmid))
        return bool(r.status_code == 200 and (r.json() or {}).get("running"))
    except Exception:
        return False


def _live_fps(ip):
    """fps lu par l'encodeur monitor (:8080) — > 0 = des frames circulent donc le flux
    est effectivement publié vers la passerelle (path live)."""
    if not ip:
        return 0.0
    try:
        r = requests.get(f"http://{ip}:8080/", timeout=1.5)
        if r.status_code == 200:
            return float((r.json() or {}).get("fps") or 0.0)
    except Exception:
        pass
    return 0.0


def status(uid):
    c = _container_for(uid)
    ip = (c or {}).get("ip")
    if c and not ip:
        from .addressing import get_container_ip
        ip = get_container_ip(c["vmid"])
    running = _script_running(ip, c.get("vmid")) if c else False
    fps = _live_fps(ip) if running else 0.0
    return {
        "gateway_ready": gateway_ready(),
        "exists": bool(c),
        "vmid": (c or {}).get("vmid"),
        "ip": ip,
        "script_running": running,
        "publishing": fps > 0,    # flux effectivement poussé vers la passerelle
        "live_fps": fps,
        "embed_url": _embed_url(uid),
        "path": _path(uid),
        "source": _source.get(uid),
    }


def create_iter(uid):
    """Générateur streamé (contrat ✅/❌) : crée le container monitor Docker de l'utilisateur
    et y déploie l'encodeur streamer."""
    yield f"Création du monitor de l'utilisateur #{uid}…"
    if not gateway_ready():
        yield "❌ La passerelle WebRTC n'est pas déployée/activée (Réglages → WebRTC)."
        return

    c = _container_for(uid)
    if c:
        target = c["vmid"]
        yield f"Réutilisation du container monitor existant #{target}."
    else:
        yield f"Création du container Docker {_hostname(uid)}…"
        created = False
        try:
            # PAS de yield sous le verrou (un client HTTP bloqué garderait le lock).
            with _create_lock:
                c = _container_for(uid)      # re-check sous verrou (double-clic / 2 onglets)
                if c:
                    target = c["vmid"]
                else:
                    target = _create_monitor_container(_hostname(uid))
                    if target:
                        created = True
                        try: db_set_monitor_user(target, int(uid))
                        except Exception: pass
        except RuntimeError as e:
            yield f"❌ {e}"
            return
        if not target:
            yield "❌ Création du container échouée (voir alertes)."
            return
        yield (f"  → container #{target} créé." if created
               else f"Réutilisation du container monitor existant #{target}.")

    yield "Déploiement de l'encodeur monitor…"
    ok = deployer_script(target, "streamer", _params(uid))
    if not ok:
        yield "❌ Déploiement de l'encodeur échoué."
        return
    touch(uid)
    yield f"✅ Monitor prêt (#{target}). Sélectionnez une source via un bouton « Monitoring »."


def _warm_source(uid, c):
    """Restaure _source[uid] depuis le deploy_config du monitor après un restart.
    Permet de hot-swapper immédiatement sans redéploiement inutile."""
    if uid in _source:
        return
    import json as _json
    dc_raw = c.get("deploy_config")
    try:
        dc = _json.loads(dc_raw) if isinstance(dc_raw, str) else (dc_raw or {})
    except Exception:
        return
    p = dc.get("params") or {}
    shm_name = p.get("shm_name") or ""
    if not p.get("hot_input") or not shm_name:
        return
    vid = p.get("video") or {}
    w = int(vid.get("width") or 0)
    h = int(vid.get("height") or 0)
    if not w or not h:
        return
    chroma     = str(p.get("chroma") or vid.get("chroma") or "422")
    bit_depth  = int(p.get("bit_depth") or vid.get("bit_depth") or 8)
    scan       = str(p.get("scan") or "p")
    field_order= str(p.get("field_order") or "")
    key = (w, h, chroma, bit_depth, scan, field_order)
    _source[uid] = {
        "shm":       shm_name,
        "label":     shm_name,
        "audio_shm": p.get("audio_shm") or None,
        "w": w, "h": h,
        "fmt_key":   key,
        "fmt_key_db": _fmt_key6(_shm_fmt(shm_name)) or key,
        "hot":       True,
    }


def _fmt_key6(fmt):
    """Clé de comparaison de format COMPLÈTE (6-uplet w/h/chroma/profondeur/scan/field_order).
    Toujours passer par ce helper : comparer des tuples de tailles différentes rend le
    hot-swap/le suiveur de résolution systématiquement « différent » (churn permanent)."""
    if not fmt:
        return None
    return (fmt.get("w"), fmt.get("h"), fmt.get("chroma"), fmt.get("bit_depth"),
            fmt.get("scan"), fmt.get("field_order"))


def set_source(uid, shm, label=None, audio_shm=None, *, _touch=True):
    """Re-pointe l'encodeur monitor sur `shm` (+ `audio_shm` optionnel).

    Format vidéo connu + audio inchangé + même résolution → **hot-swap** via :8082/input :
    même ffmpeg, même path WebRTC, **zéro coupure**. Sinon (résolution inconnue/différente,
    ou audio_shm change) → redéploiement (stop+start)."""
    c = _container_for(uid)
    if not c:
        return {"need_create": True}
    ip = c.get("ip")
    if not ip:
        from .addressing import get_container_ip
        ip = get_container_ip(c["vmid"])

    _warm_source(uid, c)    # restaure _source depuis DB si vide (après restart)

    db_fmt = _shm_fmt(shm)                               # format côté DB (référence du suiveur)
    fmt_video = _shm_fmt_node(c, shm) or db_fmt          # SOURCE DE VÉRITÉ = flowDef réel sur le nœud ; repli config DB
    prev = _source.get(uid) or {}
    # Hot-swap seulement si le FORMAT COMPLET est identique (résolution/chroma/profondeur/scan) :
    # un changement de chroma/profondeur/balayage impose un redéploiement (ffmpeg relit le layout
    # ET la chaîne de filtre — un passage progressif↔entrelacé doit (dé)activer le bwdif).
    cur_key = _fmt_key6(fmt_video)
    running = _script_running(ip, c.get("vmid"))
    # Hot-swap possible si : format connu + running + même fmt + audio_shm INCHANGÉ.
    # Changer l'audio_shm nécessite un redéploiement (audio_feeder doit être recâblé).
    # Audio → vidéo-seule (audio_shm None→None) : hot, l'audio_feeder envoie silence.
    # Même audio_shm (ex. 2110_audio_0 → 2110_audio_0) : hot, seule la vidéo change.
    hot_ok = (
        fmt_video is not None
        and running
        and prev.get("fmt_key") is not None
        and prev.get("fmt_key") == cur_key
        and (audio_shm or None) == prev.get("audio_shm")
    )

    _source[uid] = {"shm": shm, "label": label or shm, "audio_shm": audio_shm or None,
                    "w": fmt_video["w"] if fmt_video else None, "h": fmt_video["h"] if fmt_video else None,
                    "fmt_key": cur_key, "fmt_key_db": _fmt_key6(db_fmt),
                    "hot": fmt_video is not None}
    if _touch:
        touch(uid)    # le chemin auto-follow ne doit PAS rafraîchir _last_used (sinon reaper inopérant)

    if hot_ok:
        try:
            r = requests.post(f"http://{ip}:8082/input", json={"shm": shm}, timeout=3)
            if r.status_code == 200:
                return {"ok": True, "hot": True, "embed_url": _embed_url(uid), "source": _source[uid]}
        except Exception as e:
            log.warning(f"monitor hot-swap {c['vmid']}: {e}")
        # échec hot → on retombe sur le redéploiement ci-dessous

    if fmt_video is not None:
        params = _params(uid, shm, fmt=fmt_video, hot=True, audio_shm=audio_shm or None)
    else:
        params = _params(uid, shm, audio_shm=audio_shm)             # auto-dims (res inconnue)
    threading.Thread(
        target=deployer_script,
        args=(c["vmid"], "streamer", params),
        daemon=True).start()
    return {"ok": True, "hot": False, "embed_url": _embed_url(uid), "source": _source[uid]}


def activate(uid):
    """Réactive le script monitor (start) s'il a été coupé par le reaper."""
    c = _container_for(uid)
    if not c:
        return {"need_create": True}
    ip = c.get("ip")
    if not ip:
        from .addressing import get_container_ip
        ip = get_container_ip(c["vmid"])
    touch(uid)
    if ip and not _script_running(ip, c.get("vmid")):
        try:
            deploy.agent_session().post(deploy.agent_url(ip, "/start"), timeout=5,
                                        headers=deploy.agent_headers(c.get("vmid")))
        except Exception as e:
            log.warning(f"monitor activate {c['vmid']}: {e}")
    return {"ok": True, "embed_url": _embed_url(uid)}


def destroy(uid):
    """Détruit le container monitor de l'utilisateur (depuis Réglages → Utilisateurs)."""
    c = _container_for(uid)
    if not c:
        return {"ok": True, "existed": False}
    vmid = c["vmid"]
    _source.pop(uid, None)
    _last_used.pop(uid, None)
    threading.Thread(target=detruire_container, args=(vmid,), daemon=True).start()
    return {"ok": True, "existed": True, "vmid": vmid}


def _reap_once():
    now = time.time()
    for uid, ts in list(_last_used.items()):
        if now - ts <= IDLE_TIMEOUT:
            continue
        c = _container_for(uid)
        ip = (c or {}).get("ip")
        if c and not ip:
            from .addressing import get_container_ip
            ip = get_container_ip(c["vmid"])
        if c and ip and _script_running(ip, c.get("vmid")):
            try:
                deploy.agent_session().post(deploy.agent_url(ip, "/stop"), timeout=5,
                                            headers=deploy.agent_headers(c.get("vmid")))
                log.info(f"monitor: script utilisateur #{uid} coupé (inactif > {IDLE_TIMEOUT}s)")
            except Exception as e:
                # Stop non confirmé (agent injoignable…) : on GARDE l'entrée pour retenter
                # au prochain tick — sinon l'encodeur tourne pour toujours.
                log.warning(f"monitor reap {uid}: {e} (nouvel essai au prochain cycle)")
                continue
        _last_used.pop(uid, None)


def _reaper_loop():
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            _reap_once()
        except Exception as e:
            log.warning(f"monitor reaper: {e}")


# ── Auto-suivi de la résolution de la source ──────────────────────────────────
# La résolution d'un receiver MTL n'est connue qu'à l'activation du SDP (ex. 720p par défaut
# à la création → 1080p quand la source arrive). Un moniteur pointé AVANT l'activation reste
# figé (hot, dims fixes) sur l'ancienne résolution → image cassée. Cette boucle re-vérifie
# périodiquement le format de la source des moniteurs actifs et les RE-DÉPLOIE si ça a changé.
FOLLOW_INTERVAL = 5


def _follow_resolution(uid):
    src = _source.get(uid)
    if not src or not src.get("shm") or src.get("audio_shm") or not src.get("hot"):
        return                       # seul le mode hot vidéo-seule peut se périmer
    fmt = _shm_fmt(src["shm"])
    if not fmt:
        return
    # Comparer DB↔DB : `fmt_key` (nœud) peut légitimement diverger du format DB — comparer
    # le format DB courant au fmt_key nœud provoquait un redeploy toutes les 5 s.
    cur_key = _fmt_key6(fmt)
    if cur_key != src.get("fmt_key_db"):
        log.info(f"monitor #{uid}: format source {src.get('fmt_key_db')} → {cur_key}, redeploy auto")
        # _touch=False : le suivi automatique ne compte pas comme activité utilisateur
        # (sinon _last_used est rafraîchi toutes les 5 s et le reaper ne coupe jamais rien).
        set_source(uid, src["shm"], src.get("label"), _touch=False)


def _follow_loop():
    while True:
        time.sleep(FOLLOW_INTERVAL)
        for uid in list(_source):
            if time.time() - _last_used.get(uid, 0) > IDLE_TIMEOUT:
                continue             # ne suit que les moniteurs récemment actifs
            try:
                _follow_resolution(uid)
            except Exception as e:
                log.warning(f"monitor follow {uid}: {e}")


def start_reaper():
    threading.Thread(target=_reaper_loop, daemon=True).start()
    threading.Thread(target=_follow_loop, daemon=True).start()


# ── Monitors dédiés par player ────────────────────────────────────────────────
# Contrairement aux monitors utilisateur (éphémères, reapés), ces containers
# encodent en permanence la sortie d'un player spécifique. Hostname :
# mon_<projet>_<hostname_player> (sanitisé). Ils ne sont pas reapés.

import re as _re


def _san(s):
    s = _re.sub(r'[^a-z0-9]', '-', (s or "").lower().strip())
    return _re.sub(r'-+', '-', s).strip('-')


def dedicated_hostname(player_hostname, project_name=None):
    proj = _san(project_name)
    hn   = _san(player_hostname)
    return f"mon-{proj}-{hn}" if proj else f"mon-{hn}"


def dedicated_webrtc_path(player_hostname):
    return "mon-" + _re.sub(r'[^a-z0-9-]', '-', (player_hostname or "").lower())


def dedicated_embed_url(player_hostname):
    ip = st.get("webrtc_gateway_ip")
    if not ip:
        return None
    port = int(st.get("webrtc_http_port") or 8889)
    return f"http://{ip}:{port}/{dedicated_webrtc_path(player_hostname)}"


def _dedicated_container(player_vmid):
    """Retrouve le container monitor dédié au player vmid (par hostname calculé)."""
    from .database import db_get_projects
    c = db_get_container(player_vmid)
    if not c:
        return None, None
    hn  = c.get("hostname") or f"player{player_vmid}"
    pid = c.get("project_id")
    proj_name = None
    if pid:
        for p in db_get_projects():
            if p["id"] == pid:
                proj_name = p["name"]
                break
    target_hn = dedicated_hostname(hn, proj_name)
    for cont in db_get_containers():
        if cont.get("hostname") == target_hn:
            return cont, target_hn
    return None, target_hn


def dedicated_status(player_vmid):
    """Statut du monitor dédié : exists, hostname, vmid, status, embed_url."""
    cont, target_hn = _dedicated_container(player_vmid)
    player = db_get_container(player_vmid) or {}
    player_hn = player.get("hostname") or f"player{player_vmid}"
    if not cont:
        return {
            "exists": False,
            "hostname": target_hn,
            "embed_url": dedicated_embed_url(player_hn),
        }
    return {
        "exists": True,
        "hostname": target_hn,
        "vmid": cont["vmid"],
        "status": cont.get("status"),
        "embed_url": dedicated_embed_url(player_hn),
    }


def create_dedicated_iter(player_vmid, shm=None, audio_shm=None):
    """Générateur streamé : crée le container monitor Docker dédié au container `player_vmid`.

    `shm` / `audio_shm` explicites quand la source N'EST PAS déductible du hostname. C'était le
    cas du player (`<hn>_0` / `<hn>_audio_0`) et ça le reste par défaut ; mais un aperçu peut
    porter sur ce qu'un conteneur REGARDE plutôt que sur ce qu'il produit — un scope ne produit
    rien, il mesure une source câblée ailleurs. Deviner à partir du hostname donnerait un shm
    inexistant et un aperçu noir sans cause lisible.

    Idempotent : rappelé avec un `shm` différent, il RE-DÉPLOIE l'encodeur existant sur la
    nouvelle source au lieu de créer un second conteneur."""
    from .database import db_get_projects

    if not gateway_ready():
        yield "❌ La passerelle WebRTC n'est pas déployée/activée (Réglages → WebRTC)."
        return

    player = db_get_container(player_vmid)
    if not player:
        yield f"❌ Player #{player_vmid} introuvable."
        return

    player_hn = player.get("hostname") or f"player{player_vmid}"
    pid = player.get("project_id")
    proj_name = None
    if pid:
        for p in db_get_projects():
            if p["id"] == pid:
                proj_name = p["name"]
                break

    target_hn = dedicated_hostname(player_hn, proj_name)
    yield f"Monitor dédié : {target_hn}"

    # Réutiliser si déjà existant
    cont, _ = _dedicated_container(player_vmid)
    if cont:
        target = cont["vmid"]
        yield f"Container #{target} déjà existant — redéploiement du script."
    else:
        yield f"Création du container Docker {target_hn}…"
        created = False
        try:
            # PAS de yield sous le verrou (cf. create_iter).
            with _create_lock:
                cont, _ = _dedicated_container(player_vmid)   # re-check sous verrou
                if cont:
                    target = cont["vmid"]
                else:
                    target = _create_monitor_container(target_hn)
                    created = bool(target)
        except RuntimeError as e:
            yield f"❌ {e}"
            return
        if not target:
            yield "❌ Création du container échouée (voir alertes)."
            return
        yield (f"  → container #{target} créé." if created
               else f"Container #{target} déjà existant — redéploiement du script.")

    # Déploiement
    yield "Déploiement de l'encodeur…"
    path = dedicated_webrtc_path(player_hn)
    vid_shm  = shm or f"{player_hn}_0"
    aud_shm  = audio_shm if shm else f"{player_hn}_audio_0"
    fmt_ded  = _shm_fmt(vid_shm) or {}
    params = _params(
        uid=0,
        shm=vid_shm,
        audio_shm=aud_shm,
        fmt=fmt_ded if fmt_ded.get("w") else None,
        hot=bool(fmt_ded.get("w")),
    )
    params["destinations"] = [{"type": "webrtc", "path": path, "enabled": True}]
    ok = deployer_script(target, "streamer", params)
    if not ok:
        yield "❌ Déploiement de l'encodeur échoué."
        return
    yield f"✅ Monitor dédié prêt (#{target})."
