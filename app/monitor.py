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


def _create_monitor_container(hostname, deploy_type="streamer", prefer_node=None):
    """Crée un container monitor Docker compute. Lève RuntimeError si aucun nœud disponible.
    `prefer_node` : le nœud de la SOURCE, quand on la connaît — l'encodeur y lit le bus local au
    lieu d'attendre une réplique RDMA (cf. create_dedicated_iter)."""
    from .docker_compute import pick_compute_node, creer_container_compute
    node_id = pick_compute_node(prefer_id=prefer_node, deploy_type=deploy_type)
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


# ── Monitoring FLUIDE (préférence PAR UTILISATEUR) ─────────────────────────────────────────────
# Par défaut le moniteur sort à 25 i/s, quelle que soit la source : c'est le choix de l'exploitation
# (un encodeur par utilisateur connecté, souvent logiciel). « Fluide » = la cadence de sortie SUIT
# la cadence déclarée de la source (50p → 50), plafonnée à FLUID_MAX_FPS, GOP = 1 s à cette cadence.
# Coût : ~2× d'encodage et 1,5 à 2× de débit à 50 — sensible en x264, d'où l'option et pas le défaut.
# Persisté côté serveur (réglage `monitor_fluid_users` = liste d'uid) : il suit l'utilisateur d'un
# navigateur à l'autre et survit à la destruction de son conteneur moniteur.
DEFAULT_FPS = 25
FLUID_MAX_FPS = 60
FLUID_SETTING = "monitor_fluid_users"
_fluid_lock = threading.Lock()   # lecture-modification-écriture de la liste (deux bascules concurrentes)


def _rate_value(v):
    """Cadence déclarée → float (> 0) ou None. Accepte 25, 29.97, "50", "30000/1001", "50/1"."""
    if v is None or v == "":
        return None
    try:
        s = str(v).strip()
        if "/" in s:
            n, d = s.split("/", 1)
            r = float(n) / float(d or 1)
        else:
            r = float(s)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def output_rate(fmt, fluid):
    """(fps, gop) de SORTIE du moniteur. Entiers : le normaliseur du streamer
    (scripts.normalize_worker_udp_params) arrondit `video.fps` à l'entier — 59.94 sort à 60, soit
    une image répétée toutes les ~17 s, invisible sur un aperçu (le moniteur échantillonne de toute
    façon la DERNIÈRE image du bus à sa propre horloge).
    - défaut, ou cadence de source inconnue : DEFAULT_FPS ;
    - source ENTRELACÉE : DEFAULT_FPS — le streamer tisse les deux champs puis désentrelace à la
      cadence TRAME (bwdif send_frame) ; pas de désentrelacement à la cadence champ ici."""
    if not fluid or not fmt:
        return DEFAULT_FPS, DEFAULT_FPS
    if str(fmt.get("scan") or "p").strip().lower().startswith("i"):
        return DEFAULT_FPS, DEFAULT_FPS
    r = _rate_value(fmt.get("fps"))
    if not r:
        return DEFAULT_FPS, DEFAULT_FPS
    fps = max(1, int(round(min(r, FLUID_MAX_FPS))))
    return fps, fps


BASE_BITRATE_KBPS = 8000   # débit du moniteur à 25 i/s (cf. _params : en dessous, macroblocs en 1080)


def output_bitrate(fps):
    """Débit de SORTIE pour la cadence `fps`. L'encodeur est en débit CONSTANT (NVENC `-rc cbr`,
    x264 `-b:v`) : à 50 i/s sous le même débit, chaque image reçoit moitié moins de bits — mesuré
    le 2026-10-12 (1131, NVENC) : 8,3 Mb/s à 25 comme à 50. On donne ×(0,5 + 0,5·fps/25), soit ×1,5
    à 50 et ×1,7 à 60 (deux fois plus d'images ne demandent pas deux fois plus de bits : l'écart
    entre deux images consécutives est moitié moindre). Cadence ≤ 25 : débit de base, inchangé."""
    k = max(1.0, 0.5 + 0.5 * float(fps or DEFAULT_FPS) / DEFAULT_FPS)
    return "8M" if k == 1.0 else "%dk" % int(round(BASE_BITRATE_KBPS * k))


def fluid_enabled(uid):
    try:
        return int(uid) in {int(x) for x in (st.get(FLUID_SETTING) or [])}
    except (TypeError, ValueError):
        return False


def _store_fluid(uid, on):
    with _fluid_lock:
        try:
            cur = {int(x) for x in (st.get(FLUID_SETTING) or [])}
        except (TypeError, ValueError):
            cur = set()
        (cur.add if on else cur.discard)(int(uid))
        st.set(FLUID_SETTING, sorted(cur))


def set_fluid(uid, on, *, _touch=True):
    """Bascule « monitoring fluide » de l'utilisateur et l'APPLIQUE tout de suite : la source
    courante est re-pointée (set_source), ce qui redéploie l'encodeur si la cadence de sortie
    change (25p : rien ne change, rien n'est redémarré)."""
    on = bool(on)
    _store_fluid(uid, on)
    out = {"ok": True, "fluid": on, "restarted": False}
    c = _container_for(uid)
    if not c:
        return out
    _warm_source(uid, c)
    src = _source.get(uid) or {}
    if not src.get("shm"):
        return out
    r = set_source(uid, src["shm"], src.get("label"), src.get("audio_shm"), _touch=_touch)
    out["restarted"] = bool(r.get("ok")) and not r.get("hot")
    out["out_fps"] = (_source.get(uid) or {}).get("out_fps")
    return out


def _params(uid, shm="", audio_shm=None, fmt=None, hot=False, fluid=False):
    """Params du monitor : encode `shm` (vidéo) + `audio_shm` optionnel en H.264/Opus et pousse
    un leg WebRTC sur le path per-user. Le format d'ENTRÉE (résolution, chroma, profondeur,
    colorimétrie) SUIT la source (`fmt` issu de `_shm_fmt`) — aucune valeur statique : un
    consommateur doit lire le shm avec le format réel du producteur, pas des défauts.

    - hot=True : hot-input (source re-câblable via :8082 sans redéployer), dims FIXES (fmt requis).
    - sinon : fmt absent → width/height=0 → auto-détection (dernier recours, résolution inconnue)."""
    f = fmt or {}
    chroma = f.get("chroma") or "422"
    out_fps, gop = output_rate(fmt, fluid)
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
        # Qualité : 8 Mb/s, NVENC p4 (x264 superfast sans carte). À 2 Mb/s en NVENC p1, une image
        # 1080 sombre et détaillée sortait en macroblocs — alors que la trame lue sur le bus,
        # capturée en PNG, était propre (constaté le 2026-10-11). `tune ull` garde la latence basse.
        "video": {"codec": "h264", "bitrate": output_bitrate(out_fps), "preset": "superfast",
                  "encoder": "auto", "nvenc_preset": "p4", "nvenc_tune": "ull",
                  "gop": gop, "width": int(f.get("w") or 0), "height": int(f.get("h") or 0),
                  # Chroma de SORTIE : 4:2:0, quel que soit celui de la source (l'entrée, elle,
                  # suit la source — clé `chroma` du haut). Le WebRTC des navigateurs ne décode que
                  # du H.264 4:2:0 ; une source 4:2:2 partait en « High 4:4:4 Predictive »
                  # (`h264_nvenc` ne sait pas le 4:2:2, ffmpeg choisissait le 4:4:4 seul).
                  "fps": out_fps, "chroma": "420", "colorimetry": f.get("colorimetry") or ""},
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
        "fluid": fluid_enabled(uid),
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
        "out_fps":   int(vid.get("fps") or DEFAULT_FPS),
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
    # Cadence de SORTIE : 25, ou celle de la source en mode fluide. Le flowDef du nœud peut ne pas
    # porter de cadence lisible : repli sur la cadence déclarée en base.
    _rate_fmt = dict(fmt_video or db_fmt or {})
    if fmt_video and not _rate_value(fmt_video.get("fps")) and db_fmt:
        _rate_fmt["fps"] = db_fmt.get("fps")
    fluid = fluid_enabled(uid)
    out_fps = output_rate(_rate_fmt or None, fluid)[0]
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
        # Cadence de sortie inchangée : passer d'une source 25p à une 50p en mode fluide (ou basculer
        # l'option) change `-r` et le GOP de ffmpeg → redéploiement, pas de hot-swap.
        and prev.get("out_fps", DEFAULT_FPS) == out_fps
    )

    _source[uid] = {"shm": shm, "label": label or shm, "audio_shm": audio_shm or None,
                    "w": fmt_video["w"] if fmt_video else None, "h": fmt_video["h"] if fmt_video else None,
                    "fmt_key": cur_key, "fmt_key_db": _fmt_key6(db_fmt),
                    "hot": fmt_video is not None, "out_fps": out_fps}
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
        params = _params(uid, shm, fmt=_rate_fmt, hot=True, audio_shm=audio_shm or None,
                         fluid=fluid)
    else:
        params = _params(uid, shm, audio_shm=audio_shm)             # auto-dims (res inconnue) → 25
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


# ── Aperçu PAR SORTIE (2026-10-20, panneaux de workspace) ─────────────────────────────────────
# Le moniteur dédié est indexé par CONTENEUR : une source, qu'on re-pointe (un scope montre ce qu'il
# mesure). Un mélangeur a pourtant plusieurs sorties à regarder EN MÊME TEMPS (PGM et PVW). `suffix`
# — demandé explicitement, jamais déduit — donne un moniteur PAR SORTIE : `mon-<proj>-<hn>-<suffix>`,
# chemin WebRTC `mon-<hn>-<suffix>`. Sans suffixe, noms et comportement d'avant, inchangés.
def flow_suffix(hostname, shm):
    """« rl-mix_pvw » d'un conteneur « rl-mix » → « pvw » ; sa sortie principale (« rl-mix ») → None."""
    hn, sh = (hostname or ""), (shm or "")
    if not sh or sh == hn:
        return None
    rest = sh[len(hn) + 1:] if sh.startswith(hn + "_") else sh
    return _san(rest) or None


def dedicated_hostname(player_hostname, project_name=None, suffix=None):
    proj = _san(project_name)
    hn   = _san(player_hostname) + ("-" + _san(suffix) if suffix else "")
    return f"mon-{proj}-{hn}" if proj else f"mon-{hn}"


def dedicated_webrtc_path(player_hostname, suffix=None):
    return "mon-" + _re.sub(r'[^a-z0-9-]', '-', (player_hostname or "").lower()) + ("-" + _san(suffix) if suffix else "")


def dedicated_embed_url(player_hostname, suffix=None):
    ip = st.get("webrtc_gateway_ip")
    if not ip:
        return None
    port = int(st.get("webrtc_http_port") or 8889)
    return f"http://{ip}:{port}/{dedicated_webrtc_path(player_hostname, suffix)}"


def _dedicated_container(player_vmid, suffix=None):
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
    target_hn = dedicated_hostname(hn, proj_name, suffix)
    for cont in db_get_containers():
        if cont.get("hostname") == target_hn:
            return cont, target_hn
    return None, target_hn


def dedicated_status(player_vmid, suffix=None):
    """Statut du monitor dédié : exists, hostname, vmid, status, embed_url."""
    cont, target_hn = _dedicated_container(player_vmid, suffix)
    player = db_get_container(player_vmid) or {}
    player_hn = player.get("hostname") or f"player{player_vmid}"
    if not cont:
        return {
            "exists": False,
            "hostname": target_hn,
            "embed_url": dedicated_embed_url(player_hn, suffix),
        }
    return {
        "exists": True,
        "hostname": target_hn,
        "vmid": cont["vmid"],
        "status": cont.get("status"),
        "embed_url": dedicated_embed_url(player_hn, suffix),
    }


def _produced_av(cont, hostname):
    """(vidéo, audio) PUBLIÉS par le conteneur, lus dans le `wiring.produces` de son plugin.
    Le défaut supposait `<hn>_0` / `<hn>_audio_0` ; le player publie `<hn>` / `<hn>_audio` — le
    moniteur dédié encodait un flux inexistant et la passerelle répondait « stream not found »
    (constaté le 2026-10-11)."""
    import json as _json
    from . import plugins
    dc = cont.get("deploy_config") or {}
    if isinstance(dc, str):
        try:
            dc = _json.loads(dc)
        except ValueError:
            dc = {}
    w = plugins.derive_wiring(dc.get("type") or "", hostname, dc.get("params") or {})
    prods = [p for p in w.get("produces") or [] if p.get("shm")]
    first = lambda e: next((p["shm"] for p in prods if p.get("essence", p.get("kind")) == e), None)
    # Un flux audio d'ÉCOUTE déclaré (`monitor: true`, ex. la sortie monitoring du player) passe
    # avant la sortie antenne : c'est lui que l'exploitant règle pour ce qu'il veut entendre.
    mon = next((p["shm"] for p in prods if p.get("monitor") and p.get("essence", p.get("kind")) == "audio"), None)
    return first("video"), mon or first("audio")


def create_dedicated_iter(player_vmid, shm=None, audio_shm=None, suffix=None):
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

    target_hn = dedicated_hostname(player_hn, proj_name, suffix)
    yield f"Monitor dédié : {target_hn}"

    # Réutiliser si déjà existant
    cont, _ = _dedicated_container(player_vmid, suffix)
    if cont:
        target = cont["vmid"]
        yield f"Container #{target} déjà existant — redéploiement du script."
    else:
        yield f"Création du container Docker {target_hn}…"
        created = False
        try:
            # PAS de yield sous le verrou (cf. create_iter).
            with _create_lock:
                cont, _ = _dedicated_container(player_vmid, suffix)   # re-check sous verrou
                if cont:
                    target = cont["vmid"]
                else:
                    # Sur le nœud du conteneur monitoré : un moniteur dédié a une seule source,
                    # connue d'avance. Placé ailleurs, il dépend d'une réplique RDMA — posé sur
                    # r620-1, le lien était créé mais jamais alimenté, et l'aperçu restait sur
                    # « stream not found » (constaté le 2026-10-11).
                    target = _create_monitor_container(target_hn, prefer_node=player.get("node_id"))
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
    path = dedicated_webrtc_path(player_hn, suffix)
    vid_shm, aud_shm = shm, audio_shm
    if not shm:
        vid_shm, aud_shm = _produced_av(player, player_hn)
    if not vid_shm:
        yield "❌ Ce conteneur ne publie aucun flux vidéo à monitorer."
        return
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


# ── Macros : « monitoring fluide » de l'utilisateur qui lance la macro ────────────────────────────
# Fournisseur du cœur (core_plugins.CORE_PROVIDERS). La cible est l'utilisateur de la macro
# (`ctx["user"]` = son identifiant de connexion) : une préférence personnelle n'a de sens que pour
# quelqu'un. Un déclencheur ou un pupitre n'en a pas (`trigger:…`, `skaarhoj:…`) → refus explicite.
# Pas d'ÉTAT lisible en condition : `read_state` ne reçoit que le projet, jamais l'utilisateur.
MACRO_PROVIDER = {
    "id": "monitor",
    "label": "Monitoring personnel",
    "actions": [
        {"id": "set_fluid", "label": "Monitoring fluide (suit la source) — utilisateur qui lance la macro",
         "availability": "project", "params": [
             {"key": "enabled", "label": "Fluide", "type": "bool", "default": "true"}]},
    ],
    "states": [],
}


def _macro_uid(ctx):
    from .database import db_get_user
    name = str((ctx or {}).get("user") or "").strip()
    u = db_get_user(name) if name and ":" not in name else None
    if not u:
        raise RuntimeError("monitoring fluide : préférence PERSONNELLE — la macro doit être lancée "
                           "par un utilisateur (pas par un déclencheur ni un pupitre)")
    return u["id"]


def run_action(action_id, params, ctx):
    if action_id != "set_fluid":
        raise RuntimeError("action inconnue : %s" % action_id)
    v = (params or {}).get("enabled", "true")
    on = str(v).strip().lower() in ("1", "true", "yes", "oui", "on")
    # _touch=False : une macro n'est pas une activité de l'utilisateur devant son panneau.
    return set_fluid(_macro_uid(ctx), on, _touch=False)
