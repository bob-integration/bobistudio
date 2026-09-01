# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import json
def get_default_video_format(settings_dict=None):
    """Format vidéo par défaut lu dans les réglages (video_formats / video_format_default).
    Retourne un dict {width, height, fps, scan, field_order, chroma, bit_depth, colorimetry}.
    Fallback ultime 1280×720 uniquement si la palette de réglages est vide (DB vierge).
    Fonction partagée avec docker_driver._default_video_format."""
    fmt = {"width": 1280, "height": 720, "fps": 25, "scan": "p", "field_order": "",
           "chroma": "422", "bit_depth": 10, "colorimetry": "709"}
    try:
        s = settings_dict or {}
        if not s:
            from . import settings as _st
            s = _st.all() if hasattr(_st, "all") else {}
        lines = [l for l in (s.get("video_formats") or "").split("\n") if l.strip()]
        want = (s.get("video_format_default") or "").strip()
        chosen = None
        for l in lines:
            parts = l.split(";")
            if want and parts[0].strip() == want:
                chosen = parts; break
        if chosen is None and lines:
            chosen = lines[0].split(";")
        if chosen and len(chosen) >= 4:
            fmt["width"]  = int(chosen[1]); fmt["height"] = int(chosen[2])
            fmt["fps"]    = float(chosen[3])
            if len(chosen) >= 5: fmt["scan"]        = chosen[4].strip() or "p"
            if len(chosen) >= 6: fmt["chroma"]      = chosen[5].strip() or "422"
            if len(chosen) >= 7: fmt["bit_depth"]   = int(chosen[6])
            if len(chosen) >= 8: fmt["colorimetry"] = chosen[7].strip() or "709"
    except Exception:
        pass
    if fmt["scan"] == "i":
        fmt["field_order"] = fmt.get("field_order") or "tff"
    return fmt


def multiview_output_format_defaults(settings_dict=None):
    """Défaut de FORMAT DE SORTIE d'un multiview, dérivé du réglage système de format par défaut
    (get_default_video_format) — JAMAIS de littéral en dur dans le plugin. Mappe vers les clés du
    multiview : {out_width, out_height, fps, scan}. chroma/bit_depth restent gérés par le réglage
    pipeline (mxl_pipeline_bit_depth), donc non posés ici."""
    f = get_default_video_format(settings_dict)
    return {"out_width": int(f["width"]), "out_height": int(f["height"]),
            "fps": f["fps"], "scan": f.get("scan") or "p"}


def is_portrait(params):
    """Vrai si le multiview compose en portrait (sortie tournée 90°)."""
    return str((params or {}).get("orientation") or "landscape").strip().lower() in (
        "portrait_cw", "portrait_ccw")


def multiview_output_dims(params):
    """Dims du FLUX ÉMIS par un multiview (≠ canevas de design). En portrait, le multiview compose
    dans out_width×out_height (vertical) puis tourne 90° → la sortie est SWAPPÉE (paysage). Tout
    l'aval (gating de format, NMOS, câblage, preview) doit voir ces dims tournées, pas le canevas."""
    p = params or {}
    w = int(p.get("out_width") or p.get("width") or 0)
    h = int(p.get("out_height") or p.get("height") or 0)
    return (h, w) if is_portrait(p) else (w, h)

# Chroma subsampling supportée dans tout le pipeline shm (défaut 4:2:2). Détermine le
# layout octet d'une frame vidéo en shared memory ET le -pix_fmt ffmpeg. Source de vérité
# partagée entre l'orchestrateur (normalisation/NMOS) et les scripts de plugin.
VALID_CHROMA = ("420", "422", "444")
DEFAULT_CHROMA = "422"
# Diviseurs (largeur, hauteur) de la résolution chroma par rapport au luma.
CHROMA_DIV = {"420": (2, 2), "422": (2, 1), "444": (1, 1)}
PIX_FMT_BY_CHROMA = {"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}
# Whitelists colorimétrie ffmpeg ("" = laissé à l'auto-détection ffmpeg).
VALID_PRIMARIES = ("", "bt709", "bt2020", "smpte170m", "bt470bg")
VALID_TRC = ("", "bt709", "bt2020-10", "smpte2084", "arib-std-b67")
VALID_COLORSPACE = ("", "bt709", "bt2020nc", "smpte170m")
# Encodage matériel NVENC : la nomenclature n'est PAS celle de x264 (`ultrafast`/`zerolatency` y
# sont invalides et font échouer ffmpeg). Bornées ici pour que le normaliseur refuse une valeur
# hors liste au lieu de la transmettre à l'encodeur.
VALID_NVENC_PRESETS = ("", "p1", "p2", "p3", "p4", "p5", "p6", "p7")
VALID_NVENC_TUNES = ("", "ull", "ll", "hq")

# Profondeur d'échantillonnage (ST 2110-20 broadcast = 10 bits). Métadonnée du flux : portée
# par le format/NMOS ; le pipeline simulé reste en 8 bits en mémoire (uint8).
VALID_BIT_DEPTH = (8, 10, 12)
DEFAULT_BIT_DEPTH = 10

# Colorimétrie nommée (token du tableau de formats) → triplet ffmpeg + équivalents NMOS IS-04.
# Défaut broadcast HD = BT.709 ; UHD = BT.2020 (SDR) ; +variantes HDR (PQ / HLG).
COLORIMETRY = {
    "709":     {"primaries": "bt709",    "trc": "bt709",         "colorspace": "bt709",
                "nmos_colorspace": "BT709",  "nmos_transfer": "SDR"},
    "2020":    {"primaries": "bt2020",   "trc": "bt2020-10",     "colorspace": "bt2020nc",
                "nmos_colorspace": "BT2020", "nmos_transfer": "SDR"},
    "2020pq":  {"primaries": "bt2020",   "trc": "smpte2084",     "colorspace": "bt2020nc",
                "nmos_colorspace": "BT2020", "nmos_transfer": "PQ"},
    "2020hlg": {"primaries": "bt2020",   "trc": "arib-std-b67",  "colorspace": "bt2020nc",
                "nmos_colorspace": "BT2020", "nmos_transfer": "HLG"},
    "601":     {"primaries": "smpte170m", "trc": "bt709",        "colorspace": "smpte170m",
                "nmos_colorspace": "BT601",  "nmos_transfer": "SDR"},
}
DEFAULT_COLORIMETRY = "709"


def _as_int(v, default):
    """int(v) tolérant : retombe sur `default` si v est vide/non numérique/None (jamais de
    ValueError qui remonterait en HTTP 500 depuis une entrée utilisateur du body JSON)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default):
    """float(v) tolérant : retombe sur `default` si v est vide/non numérique/None."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_chroma(v):
    """Clamp une valeur de chroma sur VALID_CHROMA (défaut DEFAULT_CHROMA = 422)."""
    v = str(v or "").strip()
    return v if v in VALID_CHROMA else DEFAULT_CHROMA


def normalize_bit_depth(v):
    """Clamp une profondeur sur VALID_BIT_DEPTH (défaut DEFAULT_BIT_DEPTH = 10)."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DEFAULT_BIT_DEPTH
    return v if v in VALID_BIT_DEPTH else DEFAULT_BIT_DEPTH


def colorimetry_color_params(token):
    """Token de colorimétrie → {color_primaries, color_trc, colorspace} ffmpeg.
    Token inconnu/vide → triplet vide (auto ffmpeg)."""
    c = COLORIMETRY.get(str(token or "").strip().lower())
    if not c:
        return {"color_primaries": "", "color_trc": "", "colorspace": ""}
    return {"color_primaries": c["primaries"], "color_trc": c["trc"], "colorspace": c["colorspace"]}


def nmos_colorimetry(primaries, trc):
    """(color_primaries, color_trc) ffmpeg → (colorspace, transfer) NMOS IS-04.
    Défaut BT709/SDR si non reconnu."""
    prim = str(primaries or "").strip().lower()
    tr = str(trc or "").strip().lower()
    cs = {"bt2020": "BT2020", "smpte170m": "BT601", "bt470bg": "BT601"}.get(prim, "BT709")
    transfer = {"smpte2084": "PQ", "arib-std-b67": "HLG"}.get(tr, "SDR")
    return cs, transfer


# ─── Mode de balayage (entrelacé / progressif) ──────────────────────────────────────────
# Source de vérité UNIQUE pour « ce format est-il entrelacé + quel ordre de champ ». Le scan
# est porté HORS-BANDE (params["scan"]/["field_order"] du deploy_config → topologie →
# consommateurs), JAMAIS dans l'en-tête shm : la trame shm reste une trame pleine, cohérent
# avec bit_depth/chroma/ring. Tout code field-aware (TX passthrough, désentrelacement preview,
# futures migrations de plugins compute) DOIT passer par ces helpers — ne pas refaire
# `scan == "i"` à la main (cf. dette de la saga bit_depth/chroma).
DEFAULT_SCAN = "p"


def is_interlaced(x):
    """True si le format/params décrit un signal entrelacé (scan == 'i'). Tolère un dict
    format ou un dict de params (clé `scan`), défaut progressif."""
    return str((x or {}).get("scan") or DEFAULT_SCAN).strip().lower() == "i"


def field_order(x):
    """Ordre de champ d'un format entrelacé : 'tff' ou 'bff'. Lit `field_order` si présent,
    sinon défaut par résolution (HD/UHD ≥720 lignes = TFF ; SD ≤576 = BFF). 1080i50 = TFF,
    576i = BFF — convention broadcast."""
    x = x or {}
    fo = str(x.get("field_order") or "").strip().lower()
    if fo in ("tff", "bff"):
        return fo
    try:
        h = int(x.get("height") or 0)
    except (TypeError, ValueError):
        h = 0
    return "bff" if 0 < h <= 576 else "tff"


def nmos_interlace_mode(x):
    """Format → valeur IS-04 `interlace_mode` : 'interlaced_tff' / 'interlaced_bff' /
    'progressive'."""
    if not is_interlaced(x):
        return "progressive"
    return "interlaced_tff" if field_order(x) == "tff" else "interlaced_bff"


def deinterlace_vf(x):
    """Fragment de filtre ffmpeg pour désentrelacer un format entrelacé en vue d'un AFFICHAGE
    (preview, multiview, transcode progressif). Vide si progressif. `send_frame` = 1 trame de
    sortie par trame d'entrée → conserve la cadence trame (25 fps pour 1080i50), JAMAIS la
    cadence champ (50) qui doublerait le débit. parity 0=tff, 1=bff."""
    if not is_interlaced(x):
        return ""
    parity = 0 if field_order(x) == "tff" else 1
    return "bwdif=mode=send_frame:parity={}".format(parity)


def _normalize_audio(a):
    """Normalise la config audio worker_udp : enabled (défaut True), bitrate, et
    `tracks` = liste de pistes, chaque piste = liste de 1 (mono) ou 2 (stéréo) indices
    de canaux d'entrée 0..7. Défaut si activé sans piste : 1 piste stéréo ch0-1."""
    a = dict(a or {})
    enabled = bool(a.get("enabled", True))   # activé par défaut
    tracks = []
    for t in (a.get("tracks") or []):
        chs = []
        for c in ((t or {}).get("channels") or []):
            try:
                ci = int(c)
            except (TypeError, ValueError):
                continue
            if 0 <= ci <= 7:
                chs.append(ci)
        chs = chs[:2]   # mono (1) ou stéréo (2)
        if chs:
            tracks.append({"channels": chs})
    if enabled and not tracks:
        tracks = [{"channels": [0, 1]}]
    return {"enabled": enabled, "codec": a.get("codec") or "aac",
            "bitrate": a.get("bitrate") or "128k", "tracks": tracks}


def normalize_worker_udp_params(params):
    """Normalize worker_udp params to the multi-destination shape, migrating the
    legacy flat shape ({shm_name,width,height,fps,bitrate,dest_ip,dest_port}).

    New shape:
      {shm_name, audio_shm, video:{codec,bitrate,preset,gop,width,height,fps},
       audio:{enabled,codec,bitrate}, destinations:[{type:udp|srt|webrtc, ...}]}

    Called both at render time (generer_script) and at deploy time (deploy.py) so
    existing flat configs keep working and get migrated on next save. New-code
    naming is English by request (CLAUDE.md French convention overridden here)."""
    p = dict(params or {})
    legacy = "destinations" not in p and "video" not in p

    # MODE TRANCHE MXL (plugin streamer ≥ 0.10) : pass-through de slice_mode/slice_lines à
    # travers la whitelist — sinon la clé serait silencieusement perdue au deploy ET au
    # round-trip de l'éditeur Streams (PUT normalisé puis sauvé). Absents → rien d'ajouté
    # (configs existantes inchangées octet pour octet).
    slice_extra = {}
    if "slice_mode" in p:
        _sm = p.get("slice_mode")
        slice_extra["slice_mode"] = (_sm.strip().lower() in ("1", "true", "yes", "on")
                                     if isinstance(_sm, str) else bool(_sm))
    if "slice_lines" in p:
        slice_extra["slice_lines"] = _as_int(p.get("slice_lines") or 36, 36)

    if legacy:
        try:
            fps = int(round(float(p.get("fps", 25) or 25))) or 25
        except (TypeError, ValueError):
            fps = 25
        destinations = []
        if p.get("dest_ip"):
            destinations.append({"type": "udp", "host": p["dest_ip"],
                                 "port": _as_int(p.get("dest_port") or 9000, 9000)})
        return {
            # "" explicite (décâblage via _apply_unwire) préservé ; défaut seulement si absent
            "shm_name": p["shm_name"] if "shm_name" in p else "mxl_mix",
            "audio_shm": None,
            # chroma top-level = layout du shm d'entrée (défaut 422) ; video.chroma = sortie encodée
            "chroma": normalize_chroma(p.get("chroma")),
            "video": {"codec": "h264", "bitrate": p.get("bitrate") or "4M",
                      "preset": "ultrafast", "gop": fps,
                      "width": _as_int(p.get("width") or 1280, 1280),
                      "height": _as_int(p.get("height") or 720, 720), "fps": fps,
                      "chroma": normalize_chroma(p.get("chroma")),
                      "bit_depth": normalize_bit_depth(p.get("bit_depth")),
                      "color_primaries": "", "color_trc": "", "colorspace": ""},
            "audio": _normalize_audio(None),
            "destinations": destinations,
            **slice_extra,
        }

    v = dict(p.get("video") or {})
    try:
        fps = int(round(float(v.get("fps", 25) or 25))) or 25
    except (TypeError, ValueError):
        fps = 25
    # width/height/fps = FORMAT DE SORTIE souhaité (toujours appliqué par l'encodeur).
    # 0 explicite (largeur/hauteur) => « suivre l'entrée » (pas de mise à l'échelle) ;
    # absent (None) => défaut 1280x720. L'ENTRÉE est auto-détectée par l'encodeur et
    # ajustée vers cette sortie (cf. worker_udp._video_filter / _detect_dims).
    _w = v.get("width"); _h = v.get("height")

    def _wl(val, allowed):
        val = str(val or "").strip()
        return val if val in allowed else ""

    video = {
        "codec": v.get("codec") if v.get("codec") in ("h264", "h265") else "h264",
        "bitrate": v.get("bitrate") or "4M",
        "preset": v.get("preset") or "ultrafast",
        "gop": _as_int(v.get("gop") or fps, fps) or fps,
        "width": _as_int(_w, 1280),
        "height": _as_int(_h, 720),
        "fps": fps,
        # chroma de SORTIE (encode) ; color_* vides => laissés à l'auto ffmpeg
        "chroma": normalize_chroma(v.get("chroma")),
        "bit_depth": normalize_bit_depth(v.get("bit_depth")),
        "color_primaries": _wl(v.get("color_primaries"), VALID_PRIMARIES),
        "color_trc": _wl(v.get("color_trc"), VALID_TRC),
        "colorspace": _wl(v.get("colorspace"), VALID_COLORSPACE),
        # ★ Encodage matériel (streamer ≥ 0.15). Ce bloc est une LISTE BLANCHE : une clé absente
        # d'ici est silencieusement perdue au déploiement ET au round-trip de l'éditeur Streams.
        # C'est ce qui a fait qu'un `encoder: nvenc` posté n'atteignait jamais l'allocateur GPU —
        # le conteneur partait sans carte, sans que rien ne le signale. Toute future option
        # d'encodage doit être ajoutée ici en même temps que dans le manifeste.
        "encoder": (str(v.get("encoder") or "cpu").strip().lower()
                    if str(v.get("encoder") or "cpu").strip().lower() in ("cpu", "nvenc", "auto")
                    else "cpu"),
        "nvenc_preset": _wl(v.get("nvenc_preset"), VALID_NVENC_PRESETS) or "p1",
        "nvenc_tune": _wl(v.get("nvenc_tune"), VALID_NVENC_TUNES) or "ull",
    }
    audio = _normalize_audio(p.get("audio"))
    dests = []
    for d in (p.get("destinations") or []):
        d = dict(d or {})
        t = d.get("type")
        if t == "udp":
            dests.append({"type": "udp", "host": d.get("host") or "",
                          "port": _as_int(d.get("port") or 9000, 9000)})
        elif t == "srt":
            dests.append({"type": "srt", "host": d.get("host") or "",
                          "port": _as_int(d.get("port") or 9001, 9001),
                          "latency_ms": _as_int(d.get("latency_ms") or 120, 120),
                          "passphrase": d.get("passphrase") or "",
                          "streamid": d.get("streamid") or ""})
        elif t == "webrtc":
            wd = {"type": "webrtc", "path": d.get("path") or "",
                  "enabled": bool(d.get("enabled", True))}
            # champs résolus injectés par deploy.py (Phase D) — préservés tels quels
            for k in ("ingest_url", "whep_url", "embed_url"):
                if d.get(k):
                    wd[k] = d[k]
            dests.append(wd)
    return {
        "shm_name": p["shm_name"] if "shm_name" in p else "mxl_mix",
        "audio_shm": p.get("audio_shm") or None,
        # input shm chroma : suit la source (défaut 422). À défaut, aligné sur la sortie.
        "chroma": normalize_chroma(p.get("chroma") or video["chroma"]),
        "video": video,
        "audio": audio,
        "destinations": dests,
        **slice_extra,
    }



def normalize_receiver_params(params, settings=None):
    """Normalise les params 2110_io (RX) pour le rendu : clamp des comptes, dérivation de la
    config de simulation PAR SLOT (avec fallback depuis l'ancien schéma plat global), et
    pad/trunc des listes de slots à n_video/n_audio. Retourne un dict params portant
    video_count/audio_count + hostname + width/height + sim_video_slots/sim_audio_slots
    (listes de dicts). Appelée par le hook deploy de 2110_io AVANT le rendu plugin ; le plugin
    lit ces clés depuis CONFIG. Règle « ≥1 vidéo si les deux comptes sont à 0 »
    (au moins quelque chose à faire tourner) — n'affecte que le rendu, pas les compteurs NMOS
    (posés en colonnes par deploy.py avant cet appel).
    `settings` = dict DB (context["settings"]) → les valeurs par défaut (width/height/fps/…) viennent
    des préférences MXL (Réglages → MXL → Formats vidéo) plutôt que d'une valeur codée en dur."""
    p = dict(params or {})
    n_video = _as_int(p.get("video_count", 0) or 0, 0)
    n_audio = _as_int(p.get("audio_count", 0) or 0, 0)
    if n_video <= 0 and n_audio <= 0:
        n_video = 1
    sim_master = bool(p.get("sim_master", p.get("simulation")))
    vslots_in = p.get("sim_video_slots")
    aslots_in = p.get("sim_audio_slots")

    def _norm_active(v):
        return [bool(x) for x in v] if isinstance(v, list) and len(v) == 8 else [True] * 8
    def _norm_rupted(v):
        return [bool(x) for x in v] if isinstance(v, list) and len(v) == 8 else [False] * 8
    def _clamp_db(v):
        try: v = float(v)
        except (TypeError, ValueError): v = -18.0
        return max(-60.0, min(0.0, v))

    # Globaux legacy pour le fallback
    legacy_pattern = p.get("sim_video_pattern", "bars")
    legacy_freq    = _as_int(p.get("sim_audio_freq", 1000) or 1000, 1000)
    legacy_level   = _clamp_db(p.get("sim_audio_level_db", -18))
    legacy_active  = _norm_active(p.get("sim_audio_active"))
    legacy_rupted  = _norm_rupted(p.get("sim_audio_rupted"))

    if not isinstance(vslots_in, list):
        vslots_in = [{"enabled": sim_master, "pattern": legacy_pattern}
                     for _ in range(n_video)]
    if not isinstance(aslots_in, list):
        aslots_in = [{"enabled": sim_master, "freq": legacy_freq,
                      "level_db": legacy_level, "active": legacy_active,
                      "rupted": legacy_rupted} for _ in range(n_audio)]

    def _norm_v(slot):
        slot = slot or {}
        try: _isz = int(slot.get("ident_size") or 0)
        except (TypeError, ValueError): _isz = 0
        return {
            "enabled": bool(slot.get("enabled", False)) and sim_master,
            "pattern": slot.get("pattern", legacy_pattern),
            # IDENT : incrustation 3 lignes (nom/source/format) — indépendante du générateur.
            "ident": bool(slot.get("ident", False)),
            "ident_size": max(0, _isz),
        }
    def _norm_a(slot):
        slot = slot or {}
        return {
            "enabled": bool(slot.get("enabled", False)) and sim_master,
            "freq":    _as_int(slot.get("freq", legacy_freq) or legacy_freq, legacy_freq),
            "level_db": _clamp_db(slot.get("level_db", legacy_level)),
            "active":  _norm_active(slot.get("active")),
            "rupted":  _norm_rupted(slot.get("rupted")),
        }
    # Pad/trunc pour matcher exactement n_video / n_audio
    vslots = [_norm_v(vslots_in[i] if i < len(vslots_in) else None) for i in range(n_video)]
    aslots = [_norm_a(aslots_in[i] if i < len(aslots_in) else None) for i in range(n_audio)]

    _df = get_default_video_format(settings)
    p["hostname"]        = p.get("hostname", "mxl")
    p["video_count"]     = n_video
    p["audio_count"]     = n_audio
    p["width"]           = _as_int(p.get("width") or _df["width"], _df["width"])
    p["height"]          = _as_int(p.get("height") or _df["height"], _df["height"])
    p["fps"]             = _as_float(p.get("fps") or _df["fps"], _df["fps"])
    p["scan"]            = str(p.get("scan") or _df["scan"]).strip() or "p"
    p["chroma"]          = normalize_chroma(p.get("chroma") or _df["chroma"])
    p["bit_depth"]       = normalize_bit_depth(p.get("bit_depth") or _df["bit_depth"])
    p["colorimetry"]     = str(p.get("colorimetry") or _df.get("colorimetry") or DEFAULT_COLORIMETRY).strip().lower()
    p["sim_video_slots"] = vslots
    p["sim_audio_slots"] = aslots
    return p


def _render_script_service(type_script, params):
    """Script d'un type de conteneur fourni par un SERVICE (et non par un plugin).

    Résolution PAR CONVENTION : `services/<type>/` expose `render_script(params, hostname)`.
    Pas de table à maintenir — un service qui apporte un type de conteneur le rend, point.

    Pourquoi ça existe (incident Horace, 2026-07-28) : un service rend normalement son script
    lui-même et le passe en `script_content` à `deployer_script`. Mais tous les chemins qui
    redéploient SANS contenu — l'auto-réparation « script perdu » après un reboot (rootfs
    éphémère), la reprise de nœud, la re-provision des certificats mTLS — passent par
    `generer_script`, qui ne connaissait que les plugins. La passerelle WebRTC échouait donc sur
    « Type de script inconnu : webrtc_gateway » et restait morte pendant que les 11 autres
    conteneurs étaient relevés : la reprise automatique s'arrêtait à une exception près du but,
    et il fallait détruire/recréer le conteneur à la main."""
    import importlib
    import logging
    import re
    log = logging.getLogger(__name__)
    # Le nom vient de la DB : on n'importe QUE des identifiants simples (ni point, ni séparateur
    # de chemin) — un `type` fantaisiste ne doit pas pouvoir désigner un module arbitraire.
    if not re.fullmatch(r"[a-z0-9_]+", type_script or ""):
        return None
    try:
        mod = importlib.import_module("services.%s" % type_script)
    except ImportError:
        return None                     # pas un service : type réellement inconnu
    fn = getattr(mod, "render_script", None)
    if not callable(fn):
        return None
    try:
        return fn(params, params.get("hostname") or type_script)
    except Exception as e:
        log.warning("rendu du script de service %s : %s", type_script, e)
        return None


def generer_script(type_script, params, version=None):
    # Tous les types de containers sont des plugins : rendu via le manifeste (plugins.render_script).
    # `version` (optionnel) = version archivée du plugin à rappeler (défaut : courante).
    from . import plugins
    if plugins.is_plugin(type_script):
        return plugins.render_script(type_script, params, params.get("hostname", "mxl"), version)
    # …sauf les types apportés par un SERVICE (passerelle WebRTC) : sans ce repli, toute reprise
    # automatique les laisse morts (cf. _render_script_service).
    return _render_script_service(type_script, params)
