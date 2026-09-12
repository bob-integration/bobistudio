# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""API Streams (encodeur `streamer`) : liste/détail par-vmid (params normalisés + fps/destinations
live) + sauvegarde de la config d'encodage. Remap audio À CHAUD (POST :8082/audiomap) quand seuls
les indices de canaux changent (forme identique) — sinon redéploiement classique."""

import threading

from flask import jsonify, request

from . import bp
from .shared import _load_dc
from ..auth import (require_login, require_perm, check_vmid_access, scoped_project_ids,
                    vmid_project_ids)
from ..database import db_get_containers, db_get_container
from ..deploy import deployer_script


def _stream_obj(c):
    """Normalise un container streamer + fetch live (fps/destinations) depuis :8080.
    Retourne None si ce n'est pas un streamer. Partagé par /api/streams (liste) et
    /api/streams/<vmid> (par-instance, pour le control plugin)."""
    from ..scripts import normalize_worker_udp_params
    from ..addressing import get_container_ip
    import requests as _req
    dc = _load_dc(c)
    if not dc or dc.get("type") != "streamer":
        return None
    vmid = c["vmid"]
    params = normalize_worker_udp_params(dc.get("params") or {})
    # normalize ne porte pas hot_input → l'exposer pour présélectionner le mode sur la carte.
    params["hot_input"] = bool((dc.get("params") or {}).get("hot_input"))
    ip = c.get("ip") or get_container_ip(vmid)
    live = None
    if ip:
        try:
            r = _req.get(f"http://{ip}:8080/", timeout=1.5)
            if r.status_code == 200:
                live = r.json()
        except Exception:
            live = None
    # Injecter embed_url/whep_url pour les destinations WebRTC sans ces champs
    # (streamer déployé avant que la passerelle soit configurée).
    from .. import settings as _st
    gw_ip      = _st.get("webrtc_gateway_ip")
    gw_enabled = bool(_st.get("webrtc_enabled"))
    http_p     = int(_st.get("webrtc_http_port") or 8889)
    if gw_enabled and gw_ip:
        for d in (params.get("destinations") or []):
            if d.get("type") == "webrtc" and not d.get("embed_url") and d.get("path"):
                d["embed_url"] = f"http://{gw_ip}:{http_p}/{d['path']}"
                d["whep_url"]  = f"http://{gw_ip}:{http_p}/{d['path']}/whep"
    return {"vmid": vmid, "hostname": c.get("hostname"), "ip": ip,
            "status": c.get("status"), "params": params, "live": live}

@bp.route("/api/streams", methods=["GET"])
@require_login
def api_streams():
    """Liste des containers streamer (params normalisés + live)."""
    member_pids = scoped_project_ids()   # None = accès global (pas de filtre)
    rows = db_get_containers()
    if member_pids is not None:
        rows = [c for c in rows if vmid_project_ids(c["vmid"]) & member_pids]
    out = [o for o in (_stream_obj(c) for c in rows) if o]
    out.sort(key=lambda x: x["vmid"])
    return jsonify(out)

@bp.route("/api/streams/<int:vmid>", methods=["GET"])
@require_login
def api_stream_one(vmid):
    """Un seul streamer (par-vmid) — utilisé par le control plugin monté (polling)."""
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    c = db_get_container(vmid)
    obj = _stream_obj(c) if c else None
    if not obj:
        return jsonify({"error": "streamer introuvable"}), 404
    return jsonify(obj)

def _audio_only_remap(old, new):
    """True si old→new ne change QUE les indices de canaux audio : forme identique
    (nb pistes + mono/stéréo par piste), mêmes destinations / codecs / résolution /
    audio_shm. Seul cas éligible au remap À CHAUD (POST :8082/audiomap), sans restart."""
    ao, an = old.get("audio") or {}, new.get("audio") or {}
    if not (ao.get("enabled") and an.get("enabled")):
        return False
    if (old.get("video") != new.get("video")
            or old.get("destinations") != new.get("destinations")
            or (old.get("audio_shm") or None) != (new.get("audio_shm") or None)):
        return False
    if ao.get("codec") != an.get("codec") or ao.get("bitrate") != an.get("bitrate"):
        return False
    to, tn = ao.get("tracks") or [], an.get("tracks") or []
    if len(to) != len(tn):
        return False
    def _w(t):
        return 2 if len(((t or {}).get("channels") or [0])) >= 2 else 1
    return [_w(t) for t in to] == [_w(t) for t in tn]

def _hot_audiomap(ip, tracks):
    """POST :8082/audiomap. True si le remap à chaud a réussi (HTTP 200) ; False sinon
    (forme refusée 409, encodeur down, …) → l'appelant retombe sur un redéploiement."""
    try:
        import requests as _req
        r = _req.post(f"http://{ip}:8082/audiomap", json={"tracks": tracks}, timeout=2)
        return r.status_code == 200
    except Exception:
        return False

@bp.route("/api/streams/<int:vmid>", methods=["POST"])
@require_perm("containers.deploy")
def api_streams_save(vmid):
    """Save encoding + destinations for a worker_udp container and redeploy async."""
    body = request.get_json(force=True, silent=True) or {}
    c = db_get_container(vmid)
    if not c:
        return jsonify({"error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc or dc.get("type") != "streamer":
        return jsonify({"error": f"#{vmid} n'est pas un encodeur (streamer)"}), 400

    # On repart des params existants normalisés + on applique l'édition reçue.
    from ..scripts import normalize_worker_udp_params
    params = normalize_worker_udp_params(dc.get("params") or {})
    v_in = body.get("video") or {}
    if v_in.get("codec") and v_in["codec"] not in ("h264", "h265"):
        return jsonify({"error": "codec vidéo invalide (h264|h265)"}), 400
    from ..scripts import VALID_CHROMA
    if v_in.get("chroma") and str(v_in["chroma"]) not in VALID_CHROMA:
        return jsonify({"error": "chroma invalide (420|422|444)"}), 400
    # color_* : whitelistés silencieusement par normalize_worker_udp_params (invalide → "").
    # Champs numériques : refuser tôt (400 propre) une valeur non numérique plutôt que de laisser
    # normalize_worker_udp_params retomber silencieusement sur le défaut (le durcissement _as_int
    # côté scripts.py reste le filet principal contre le 500, ce test rend juste l'erreur explicite).
    def _is_num(x):
        if isinstance(x, bool):
            return False
        if isinstance(x, (int, float)):
            return True
        try:
            float(x)
            return True
        except (TypeError, ValueError):
            return False
    for k in ("gop", "width", "height", "fps"):
        if k in v_in and v_in[k] not in (None, "") and not _is_num(v_in[k]):
            return jsonify({"error": f"champ vidéo « {k} » doit être numérique"}), 400
    for k in ("codec", "bitrate", "preset", "gop", "width", "height", "fps",
              "chroma", "color_primaries", "color_trc", "colorspace"):
        if k in v_in:
            params["video"][k] = v_in[k]
    a_in = body.get("audio") or {}
    for k in ("enabled", "codec", "bitrate"):
        if k in a_in:
            params["audio"][k] = a_in[k]
    if "tracks" in a_in:
        if not isinstance(a_in["tracks"], list):
            return jsonify({"error": "audio.tracks doit être une liste"}), 400
        for t in a_in["tracks"]:
            chs = (t or {}).get("channels") or []
            if not chs or any((not isinstance(ci, int)) or ci < 0 or ci > 7 for ci in chs):
                return jsonify({"error": "canaux audio invalides (0..7, ≥1 par piste)"}), 400
        params["audio"]["tracks"] = a_in["tracks"]
    if "audio_shm" in body:   # normalement non envoyé (câblage) — toléré
        params["audio_shm"] = body["audio_shm"] or None
    if "destinations" in body:
        if not isinstance(body["destinations"], list):
            return jsonify({"error": "destinations doit être une liste"}), 400
        for d in body["destinations"]:
            d = d or {}
            if d.get("type") not in ("udp", "srt", "webrtc"):
                return jsonify({"error": "type de destination invalide (udp|srt|webrtc)"}), 400
            for k in ("port", "latency_ms"):
                if k in d and d[k] not in (None, "") and not _is_num(d[k]):
                    return jsonify({"error": f"destination : « {k} » doit être numérique"}), 400
        params["destinations"] = body["destinations"]
    # Re-normalise (coerce les types, nettoie) avant déploiement.
    params = normalize_worker_udp_params(params)
    # Mode source (normalize ne le porte pas) : valeur envoyée par la carte, sinon on
    # préserve l'existant. hot=False → « Adaptation auto » (détection + scaling).
    if "hot_input" in body:
        params["hot_input"] = bool(body["hot_input"])
    else:
        params["hot_input"] = bool((dc.get("params") or {}).get("hot_input"))

    # Chemin À CHAUD : si seuls les INDICES de canaux audio changent (forme/dest/codec/
    # résolution identiques), on ré-aiguille via POST :8082/audiomap sans tuer ffmpeg →
    # zéro coupure du flux. Sinon (ajout/suppression de piste, mono↔stéréo, codec, dest,
    # résolution) → redéploiement classique. Cf. _run_hot pour le mode source vidéo.
    old = normalize_worker_udp_params(dc.get("params") or {})
    if _audio_only_remap(old, params):
        from ..addressing import get_container_ip
        from ..database import db_update_deploy_config
        ip = c.get("ip") or get_container_ip(vmid)
        if ip and _hot_audiomap(ip, params["audio"]["tracks"]):
            db_update_deploy_config(vmid, "streamer", params)
            return jsonify({"status": "remap_a_chaud", "params": params})
        # échec hot (forme refusée / encodeur down) → on retombe sur le redéploiement

    threading.Thread(target=deployer_script, args=(vmid, "streamer", params), daemon=True).start()
    return jsonify({"status": "deploiement_en_cours", "params": params})
