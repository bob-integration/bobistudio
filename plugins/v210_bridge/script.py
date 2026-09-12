# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Pont v210 — interop MXL inter-éditeurs (R1, cf. docs/reference/MXL_INTEROP.md). BI-DIRECTIONNEL :
#
#   direction=export (défaut) : lit un flux vidéo PLANAR interne (câblé via Câbles,
#     hot-wire input_shm) et publie son MIROIR au format standard `video/v210` du SDK
#     MXL stock — lisible par un container tiers non patché sur le même domaine
#     /dev/shm/mxl. Une entrée 8 bits est promue 10 bits (<<2).
#
#   direction=import : lit un flux `video/v210` TIERS du domaine — ciblé par son
#     flowId UUID BRUT ou un nom maison (config import_flow ; GET /flows liste les
#     candidats découverts) — et publie sa conversion PLANAR interne sous {hostname}
#     (déclarée dans Câbles : consommable par toute la flotte). Profondeur de sortie
#     import_bit_depth (8 défaut = pipeline force8, v>>2 ; ou 10).
#
# L'index de grain est PROPAGÉ dans les deux sens (miroir 1:1, même grille) ; la
# conversion passe par bobimxl.v210_pack/v210_unpack (libbobi_v210 SIMD, repli numpy)
# directement dans la vue grain de sortie (zéro-copie).
#
# Limites v1 (loggées + exposées sur /state.reason) : 4:2:2 uniquement, PROGRESSIF
# uniquement (sémantique champ v210 stock à valider au banc croisé) ; grain commité
# TRAME ENTIÈRE (re-tranchage sémantique « ligne » : après le banc croisé).
#
# En export le flux miroir n'est PAS déclaré dans le wiring produces (nos readers
# attendent du planar) : nom + flowId (l'UUID à donner au tiers) sur :8082/state.
#
# Ce fichier est un TEMPLATE str.format() : SEULS {config} / {hostname} /
# {plugin_version} sont des placeholders. TOUTE autre accolade littérale ({{ }})
# doit être doublée, sinon str.format() échoue.
# ─────────────────────────────────────────────────────────────────────────────
import json
import signal
import threading
import time
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np
import bobimxl

class _StageDelayNulle:
    """Repli si le bobimxl de l'IMAGE est plus ancien que le script poussé.

    `deploy.py` pousse `bobimxl.py` à côté du script (il SHADOWE celui de l'image), mais le fait
    en BEST-EFFORT : un échec de ce push est journalisé et le déploiement continue avec le binding
    de l'image. Sans ce repli, l'absence de `StageDelay` lèverait AttributeError à l'import et
    tuerait le conteneur — pour une métrique manquante. Une mesure absente doit coûter la mesure,
    pas le module. Cf. docs/reference/LATENCE_CHAINE.md.
    """
    def observe(self, *a, **k):
        return False

    def publish(self):
        return None


def _sd_new():
    return bobimxl.StageDelay() if hasattr(bobimxl, "StageDelay") else _StageDelayNulle()



def _mxl_lib_state():
    """Variante libmxl réellement chargée (baseline / x86-64-v3) — diagnostic seul, ne doit
    JAMAIS faire échouer /state."""
    try:
        return bobimxl.lib_info()
    except Exception:
        return None

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



DIRECTION   = (str(CONFIG.get("direction") or "export").strip().lower())
IS_IMPORT   = DIRECTION == "import"
# export : nom du flux miroir v210 publié pour les tiers.
# import : nom du flux planar publié en interne = {hostname} (contrat wiring produces).
OUT_NAME    = (HOSTNAME if IS_IMPORT
               else ((CONFIG.get("out_name") or "").strip() or (HOSTNAME + "_v210")))
IMPORT_FLOW = (CONFIG.get("import_flow") or "").strip() or None
IMPORT_BD   = 10 if str(CONFIG.get("import_bit_depth") or "8").strip() == "10" else 8
POLL_V      = 0.002           # scrutation de la tête source (~500 Hz, suivi de tête)
# Watchdog source figée : flux présent mais tête immobile trop longtemps = shm recréé
# sous le même nom → close + GC + reopen (parade générations, motif delay/pyramide).
_STALE_REOPEN_NS = 500_000_000

_mxl_inst = bobimxl.Instance()

state_lock = threading.Lock()
state = {{
    "input_shm": CONFIG.get("input_shm") or None,   # export : entrée planar câblée
}}

class RollingMs:
    """Fenêtre glissante de durées (ms) — les fps/latences plugin sont TOUJOURS
    des fenêtres glissantes, jamais des cumuls (cf. convention flotte)."""
    def __init__(self, n=100):
        self.buf = deque(maxlen=n)
    def push(self, v):
        self.buf.append(float(v))
    def avg(self):
        return round(sum(self.buf) / len(self.buf), 2) if self.buf else None

meas_lock = threading.Lock()
meas = {{
    "fps": 0.0, "frame_index": 0,
    "transit": RollingMs(),       # âge du grain source à la lecture (inputs_latency_ms)
    "conv": RollingMs(),          # coût propre de la conversion (own_latency_ms)
    # DÉLAI D'ÉTAGE en TRAMES — distinct du coût de conversion ci-dessus, qui est un temps de
    # CALCUL. Ce pont PROPAGE la coordonnée source (open_grain(src_index=)) : l'écart est nul en
    # régime sain, et `propage=True` empêche ce zéro STRUCTUREL de se lire comme le zéro mesuré
    # d'un étage qui re-cadence. Cf. docs/reference/LATENCE_CHAINE.md.
    "delai_etage": _sd_new(),
    "simd": None,                 # True = libbobi_v210 chargée, False = repli numpy
    "reason": "",                 # pourquoi le pont ne tourne pas ("" = OK)
    "out_format": None,           # format publié (dict) quand le pont est actif
}}

bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    log("SIGBUS reçu — réouverture des flux MXL", "warning")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


# ─── HTTP : metrics 8080 + control 8082 ──────────────────────────────────────
def _in_ref():
    """Référence de l'entrée courante : shm câblé (export) ou flux tiers (import)."""
    if IS_IMPORT:
        return IMPORT_FLOW
    with state_lock:
        return state["input_shm"]


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        ref = _in_ref()
        with meas_lock:
            ilm = {{}}
            tr = meas["transit"].avg()
            if ref and tr is not None:
                ilm[ref] = tr
            payload = {{
                "fps": meas["fps"], "frame_index": meas["frame_index"],
                "inputs_latency_ms": ilm, "own_latency_ms": meas["conv"].avg(),
                "delai_etage_trames": meas["delai_etage"].publish(),
                "direction": DIRECTION, "out_name": OUT_NAME,
                "out_flow_id": bobimxl.flow_id(OUT_NAME),
                "simd": meas["simd"], "reason": meas["reason"],
                "plugin_version": PLUGIN_VERSION,
            }}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass


class ControlHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {{}}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {{}}

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/flows":
            # DÉCOUVERTE du domaine : les flux video/v210 candidats à l'import (l'opérateur
            # y trouve le flowId tiers à coller dans import_flow), tous éditeurs confondus.
            flows = [f for f in bobimxl.discover_flows()
                     if f.get("media_type") == "video/v210"]
            for f in flows:
                f.pop("def", None)              # résumé (le flowDef complet est verbeux)
                f["is_ours"] = (f.get("label") and
                                bobimxl.flow_id(f["label"]) == f.get("id"))
            return self._reply(200, {{"flows": flows}})
        if self.path != "/state":
            return self._reply(404, {{"error": "not found"}})
        with state_lock, meas_lock:
            # input_shm au niveau racine = contrat topologie Câbles (routes lit
            # live.get(state_field)) — sans ça l'entrée paraît débranchée.
            self._reply(200, {{
                "input_shm": state["input_shm"],
                "direction": DIRECTION,
                "import_flow": IMPORT_FLOW,
                "out_name": OUT_NAME,
                "out_flow_id": bobimxl.flow_id(OUT_NAME),
                "out_media_type": ("video/x-mxl-planar" if IS_IMPORT else "video/v210"),
                "out_format": meas["out_format"],
                "active": not meas["reason"] and meas["fps"] > 0,
                "reason": meas["reason"],
                "simd": meas["simd"],
                "fps": meas["fps"],
                "log_level": LOG_LEVEL,   # lisible en condition de macro
                "plugin_version": PLUGIN_VERSION,
                "mxl_lib": _mxl_lib_state(),
            }})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/input":
            shm = (body.get("shm") or "").strip() or None
            with state_lock:
                state["input_shm"] = shm
            return self._reply(200, {{"ok": True}})
        if self.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            ok = set_log_level(body.get("level") or body.get("log_level"))
            return self._reply(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})
        return self._reply(404, {{"error": "not found"}})

    def log_message(self, *a):
        pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
                 daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(),
                 daemon=True).start()


def _set_reason(msg):
    with meas_lock:
        if msg != meas["reason"]:
            meas["reason"] = msg
            if msg:
                log("pont v210 inactif : " + msg, "warning")


def _set_out_format(w, h, bd, media_type, fps_num, fps_den):
    with meas_lock:
        meas["out_format"] = {{"width": w, "height": h, "chroma": "422",
                              "bit_depth": bd, "media_type": media_type,
                              "fps_num": fps_num, "fps_den": fps_den}}


def _close(*handles):
    for h in handles:
        try:
            h and h.close()
        except Exception:
            pass


# ─── EXPORT : planar interne → miroir video/v210 stock ──────────────────────
def _open_export_writer(fmt):
    """Writer miroir `video/v210` (flowDef STOCK) depuis le format de l'entrée planar.
    Renvoie (writer, géométrie) ou (None, raison)."""
    if fmt.get("interlaced"):
        return None, "entrée entrelacée non supportée (v1 : progressif seul)"
    if (fmt.get("chroma") or "422") != "422":
        return None, "entrée %s non supportée (v210 = 4:2:2 seul)" % fmt.get("chroma")
    w, h = int(fmt["width"]), int(fmt["height"])
    if w % 2:
        return None, "largeur impaire %d (4:2:2)" % w
    # bit_depth du flowDef miroir = 10 (v210 natif) quel que soit l'octet interne ;
    # une entrée 8 bits est promue <<2 par v210_pack(bit_depth=8).
    writer = bobimxl.Writer(_mxl_inst, OUT_NAME, w, h, "422", 10,
                            fmt["fps_num"], fmt["fps_den"],
                            media_type="video/v210")
    geo = {{
        "w": w, "h": h, "bd_in": int(fmt.get("bit_depth") or 8),
        "in_sz": bobimxl.frame_bytes(w, h, "422", fmt.get("bit_depth") or 8),
    }}
    _set_out_format(w, h, 10, "video/v210", fmt["fps_num"], fmt["fps_den"])
    log("pont v210 EXPORT : %s (planar %db %dx%d) → %s (video/v210, flowId %s)"
        % (_in_ref(), geo["bd_in"], w, h, OUT_NAME, bobimxl.flow_id(OUT_NAME)), "info")
    return writer, geo


def _export_convert(view, geo, ovw):
    bobimxl.v210_pack(view[:geo["in_sz"]], geo["w"], geo["h"],
                      bit_depth=geo["bd_in"], out=ovw)


# ─── IMPORT : video/v210 tiers → planar interne ──────────────────────────────
def _open_import_writer(reader):
    """Writer planar interne {hostname} depuis le flowDef du flux v210 TIERS (lu par-flowId).
    Renvoie (writer, géométrie) ou (None, raison)."""
    fd = _mxl_inst.flow_def(fid=reader.fid)
    if not fd:
        return None, "flowDef de %s illisible (flux pas prêt ?)" % reader.fid
    mt = fd.get("media_type") or ""
    if mt != "video/v210":
        return None, "media_type %s non supporté (import v210 seul)" % (mt or "?")
    im = str(fd.get("interlace_mode") or "progressive")
    if im.startswith("interlaced"):
        return None, "flux entrelacé non supporté (v1 : progressif seul)"
    try:
        w, h = int(fd["frame_width"]), int(fd["frame_height"])
        gr = fd.get("grain_rate") or {{}}
        fn, dn = int(gr.get("numerator", 25)), int(gr.get("denominator", 1))
    except Exception:
        return None, "flowDef v210 sans géométrie exploitable"
    if w % 2:
        return None, "largeur impaire %d (4:2:2)" % w
    writer = bobimxl.Writer(_mxl_inst, OUT_NAME, w, h, "422", IMPORT_BD,
                            fn, dn)
    geo = {{
        "w": w, "h": h, "bd_in": IMPORT_BD,      # bd de SORTIE planar (8 = v>>2, 10 = brut)
        "in_sz": bobimxl.v210_frame_bytes(w, h),
    }}
    _set_out_format(w, h, IMPORT_BD, "video/x-mxl-planar", fn, dn)
    log("pont v210 IMPORT : %s (video/v210 %dx%d) → %s (planar %db, flowId %s)"
        % (reader.fid, w, h, OUT_NAME, IMPORT_BD, bobimxl.flow_id(OUT_NAME)), "info")
    return writer, geo


def _import_convert(view, geo, ovw):
    bobimxl.v210_unpack(view[:geo["in_sz"]], geo["w"], geo["h"],
                        bit_depth=geo["bd_in"], out=ovw)


# ─── Boucle commune : suivi de tête + ré-émission au même index ──────────────
def _worker():
    """Chaque nouveau grain source est ré-émis au MÊME index dans le flux de sortie,
    conversion SIMD directement dans la vue grain (zéro-copie)."""
    reader = None; writer = None; geo = None; in_ref = None
    last_idx = -1
    last_fresh_ns = time.time_ns()
    fps_t0 = time.time(); fps_n = 0
    with meas_lock:
        meas["simd"] = bobimxl._v210_load() is not None
    convert = _import_convert if IS_IMPORT else _export_convert

    while True:
        if bus_error.is_set():
            bus_error.clear()
            _close(reader, writer)
            reader = writer = geo = None; in_ref = None; last_idx = -1
            time.sleep(0.5)

        wanted = _in_ref()
        if wanted != in_ref:
            _close(reader, writer)
            reader = writer = geo = None; last_idx = -1; in_ref = wanted
        if not wanted:
            _set_reason("aucune source (câbler l'entrée)" if not IS_IMPORT
                        else "import_flow vide (flowId tiers à renseigner, cf. GET /flows)")
            time.sleep(0.05); continue
        if reader is None:
            try:
                reader = bobimxl.Reader(_mxl_inst, wanted)   # UUID littéral → par-flowId
                last_fresh_ns = time.time_ns()
            except Exception:
                _set_reason("flux source %s introuvable" % wanted)
                time.sleep(0.2); continue

        if writer is None:
            try:
                if IS_IMPORT:
                    writer, geo = _open_import_writer(reader)
                else:
                    fmt = reader.format()
                    if not fmt:
                        _set_reason("format de %s illisible (flux pas prêt ?)" % wanted)
                        time.sleep(0.2); continue
                    writer, geo = _open_export_writer(fmt)
            except Exception as e:
                writer, geo = None, "création du flux de sortie : %s" % e
            if writer is None:
                _set_reason(geo)
                time.sleep(1.0); continue
            _set_reason("")

        try:
            head = reader.head_index()
        except Exception:
            _close(reader); reader = None
            time.sleep(0.02); continue
        if head == bobimxl.MXL_UNDEFINED_INDEX:
            time.sleep(POLL_V); continue
        if head < last_idx:
            # Producteur recréé (index reparti en arrière) → resynchroniser.
            last_idx = -1
        if head == last_idx:
            if (time.time_ns() - last_fresh_ns) > _STALE_REOPEN_NS:
                # Source figée : shm probablement recréé sous le même nom → close + GC
                # + reopen (sans GC, le flux périmé reste résolvable par nom → gel).
                _close(reader, writer)
                try:
                    _mxl_inst.garbage_collect()
                except Exception:
                    pass
                reader = writer = geo = None; last_idx = -1
                last_fresh_ns = time.time_ns()
            time.sleep(POLL_V); continue

        got = reader.get_latest()
        if got is None or got[0] == last_idx:
            time.sleep(POLL_V); continue
        gidx, gi, view = got
        last_fresh_ns = time.time_ns()
        if view.size < geo["in_sz"]:
            # Grain plus court que le format annoncé (source en cours de redéploiement ?)
            # → rouvrir : le format a probablement changé.
            _close(reader, writer)
            reader = writer = geo = None; last_idx = -1
            time.sleep(0.1); continue

        t0 = time.time_ns()
        try:
            _i, ogi, ovw = writer.open_grain(src_index=gidx)
            # Sous meas_lock : tout le reste de `meas` l'est, et le handler de métriques appelle
            # publish() dans un AUTRE thread. Sans lui, publish() itérerait la fenêtre pendant
            # qu'on y pousse.
            with meas_lock:
                meas["delai_etage"].observe(writer, _i, [(reader, gidx)], propage=True)
        except Exception:
            _close(writer); writer = geo = None
            time.sleep(0.05); continue
        try:
            convert(view, geo, ovw)
        finally:
            writer.commit(ogi)     # commit garanti (grain partiel = jamais lisible)
        last_idx = gidx; fps_n += 1

        lw = reader.last_write_time()
        with meas_lock:
            meas["conv"].push((time.time_ns() - t0) / 1e6)
            if lw:
                meas["transit"].push((bobimxl.now_tai() - lw) / 1e6)
            meas["frame_index"] = gidx
        now = time.time()
        if now - fps_t0 >= 1.0:
            with meas_lock:
                meas["fps"] = round(fps_n / (now - fps_t0), 1)
            fps_t0 = now; fps_n = 0


log("pont v210 %s — %s, sortie %s (flowId %s)"
    % (PLUGIN_VERSION, DIRECTION.upper(), OUT_NAME, bobimxl.flow_id(OUT_NAME)), "info")
_worker()
