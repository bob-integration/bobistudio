***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Auteur : Cyril Mazouer, pour le compte de BOBI SAS
***REMOVED*** Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

***REMOVED*** ─────────────────────────────────────────────────────────────────────────────
***REMOVED*** Pyramide de proxies — descale chaque source vidéo YUV du pipeline MXL en
***REMOVED*** versions pré-réduites partagées (½ ¼ ⅛ 1/16). UN thread par source,
***REMOVED*** VERROUILLÉ À L'ENTRÉE 1:1 : le thread attend une NOUVELLE frame_index sur SA
***REMOVED*** source puis émet, en propageant la frame_index source (jamais de re-cadençage,
***REMOVED*** pas de grille PTP maître — sinon on perd/duplique des images).
***REMOVED***   Entrées : input_0..input_{{N-1}} (câblées à chaud, mode hot-wire)
***REMOVED***   Sorties : <source>__p2 / __p4 / __p8 / __p16  (un proxy par niveau)
***REMOVED***
***REMOVED*** MODE TRANCHE (slice_mode, chantier latence sous-trame) : une source PROGRESSIVE committée par
***REMOVED*** tranches (patch mxl-planar-slices) est suivie au grain de TÊTE et chaque proxy est écrit
***REMOVED*** BANDE PAR BANDE avec commit progressif → l'étage aval démarre sans attendre la trame complète.
***REMOVED***
***REMOVED*** Template str.format : SEULS {config} / {hostname} / {plugin_version} sont des
***REMOVED*** placeholders. TOUTE autre accolade littérale doit être doublée {{ }}.
***REMOVED*** ─────────────────────────────────────────────────────────────────────────────
import time, threading, json, os, signal, gc
from collections import deque
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
import bobimxl   ***REMOVED*** migration MXL Phase 1 : sources lues via Reader, proxies écrits via Writer

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

***REMOVED*** GC CPython DISCIPLINÉ (même remède que multiview 0.27.2, porté par uniformité) : le collect
***REMOVED*** gen2 AUTOMATIQUE tombe n'importe où dans le cycle (mesuré sur l'assembleur : pause périodique
***REMOVED*** ~34 s, ~40 ms → grain en retard, le TX aval rate sa fenêtre). On coupe le déclenchement
***REMOVED*** automatique et on collecte MANUELLEMENT : gen0/gen1 au point sûr de chaque worker (dernier
***REMOVED*** proxy committé, temps mort avant le grain source suivant) ; gen2 cadencé (~5 s) dans la boucle
***REMOVED*** d'agrégation (avec N workers non synchronisés il n'y a pas de point sûr global, mais grâce à
***REMOVED*** gc.freeze() le gen2 dure ~0,1 ms — inoffensif même à cheval sur une bande), durée mesurée →
***REMOVED*** métrique gc_full_ms sur :8080. RIEN À VOIR avec inst.garbage_collect() (GC du ring MXL), intact.
gc.collect(2)
gc.freeze()
gc.disable()

***REMOVED*** ─── Config injectée (contrat plugin) ───────────────────────
CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

***REMOVED*** ─── Niveau de log ─────────────────────────────────────────────────────────
***REMOVED*** `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
***REMOVED*** Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
***REMOVED***   debug   — le lance-flammes : par trame, par bande, décisions internes
***REMOVED***   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
***REMOVED***             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
***REMOVED***             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
***REMOVED***   warning — anomalies et replis subis
***REMOVED***   error   — échecs
***REMOVED*** RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
***REMOVED***   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
***REMOVED***   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
***REMOVED*** RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
***REMOVED***   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
***REMOVED***   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
***REMOVED***   les lignes utiles anciennes). Au mieux `debug`.
***REMOVED*** RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
***REMOVED***   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
***REMOVED***   reste, le spam disparaît.
***REMOVED*** Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
***REMOVED*** POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
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



inst = bobimxl.Instance()   ***REMOVED*** domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl)

N_INPUTS  = int(CONFIG.get("n_inputs") or 8)
OUT_RING  = max(2, int(CONFIG.get("ring") or 4))
***REMOVED*** Socle d'octaves TOUJOURS générés (filet générique) : "full" ½/¼/⅛/1/16 | "half" ½ seul | "none".
***REMOVED*** `levels` (avancé) prime s'il est fourni.
_BASE_OCTAVES = {{"full": [2, 4, 8, 16], "half": [2], "none": []}}
if CONFIG.get("levels"):
    LEVELS = [int(x) for x in CONFIG.get("levels")]
else:
    LEVELS = _BASE_OCTAVES.get(str(CONFIG.get("base_octaves") or "full").lower(), [2, 4, 8, 16])

***REMOVED*** ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
***REMOVED*** slice_mode=true → une source PROGRESSIVE est suivie au GRAIN DE TÊTE via get_slice (réveil à
***REMOVED*** chaque commit partiel du producteur — un RX 2110 slice committe ~toutes les 0,7 ms) et chaque
***REMOVED*** proxy est écrit BANDE PAR BANDE avec commit progressif (validSlices=1..N) → l'étage aval
***REMOVED*** (multiview slice) démarre sur la 1ʳᵉ bande sans attendre la trame (−~18 ms d'étage pyramide).
***REMOVED*** Convention (identique moteur/multiview) : k tranches ⇔ lignes image [0, k·slice_height)
***REMOVED*** valides SUR LES 3 PLANS (Y|Cb|Cr). Sources ENTRELACÉES : chemin historique grain-complet.
***REMOVED*** slice_mode absent/False → comportement STRICTEMENT identique à l'historique.
_slm = CONFIG.get("slice_mode", False)
SLICE_MODE  = _slm if isinstance(_slm, bool) else str(_slm).strip().lower() in ("1", "true", "yes", "on")
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
***REMOVED*** Nb de tranches CIBLE par proxy, dérivé de slice_lines (36 lignes ≈ 1080/30 → ~30 tranches) :
***REMOVED*** même granularité TEMPORELLE que le producteur amont, adaptée à la hauteur de chaque proxy.
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

***REMOVED*** Tailles sur-mesure par source : {{"<src-shm>": [[w,h], …]}} (en plus des octaves), pilotées à CHAUD
***REMOVED*** par l'orchestrateur (POST :8082/extra_sizes, reconcile_pyramide_sizes) — pas de redéploiement.
extra_lock  = threading.Lock()
_EXTRA      = dict(CONFIG.get("extra_sizes")) if isinstance(CONFIG.get("extra_sizes"), dict) else {{}}
_extra_gen  = [0]   ***REMOVED*** incrémenté à chaque maj → les workers re-synchronisent leurs proxies

def _extra_for(src):
    with extra_lock:
        return list(_EXTRA.get(src) or [])

_ecart_idx = {{}}  ***REMOVED*** nom de proxy → (index écrit − index source). 0 = propagation saine.
***REMOVED*** DÉLAI D'ÉTAGE en TRAMES, publié pour la page Câbles. La pyramide PROPAGE la coordonnée source
***REMOVED*** (open_grain(src_index=)), donc l'écart est nul en régime sain — d'où `propage=True`, qui empêche
***REMOVED*** ce zéro STRUCTUREL de se lire comme le zéro mesuré d'un étage qui re-cadence.
***REMOVED*** ⚠ On retient le PIRE proxy, pas une moyenne : `next_index` applique max(candidat, compteur+1),
***REMOVED*** donc un proxy qui a pris de l'avance une fois ne redescend JAMAIS et traîne un écart permanent
***REMOVED*** (cf. le commentaire du site d'écriture). Une moyenne noierait précisément ce proxy-là.
***REMOVED*** ⚠ UN PAR SLOT. `worker(slot)` tourne en N_INPUTS THREADS CONCURRENTS (cf. la liste `threads`
***REMOVED*** en bas de module) : un accumulateur unique mélangerait des sources sans rapport, et la valeur
***REMOVED*** publiée serait une moyenne inter-sources — exactement ce que l'avertissement ci-dessus dit de
***REMOVED*** ne pas faire. Chaque worker écrit SA clé (insertion de clés distinctes : sûre sous le GIL) et
***REMOVED*** la publication retient le PIRE.
_delai_etage = {{}}   ***REMOVED*** slot → StageDelay
***REMOVED*** DÉCLARÉ ICI, et pas près des autres accumulateurs : les workers sont démarrés
***REMOVED*** AVANT cette zone-là du module. Une déclaration tardive laisserait un worker
***REMOVED*** lever NameError dans son thread — avalé, donc invisible.
V_HEADER_SIZE = 64

***REMOVED*** ─── Kernel compose fusionné C (libbobi_mvk, chantier fusion numpy→C 2026-07) ─────────────
***REMOVED*** Chemin CPU uniquement (ce plugin n'a pas de chemin GPU, pas de garde force_cpu à appliquer,
***REMOVED*** contrairement au multiview) : place nearest (resize+assignation) en UNE passe mémoire via
***REMOVED*** bobimxl.mvk_place_into (image bobi-compute ≥ 0.11, bit-exact au numpy — mêmes formules
***REMOVED*** d'index nearest que resize_plane/_emit_band, calculées ici, le C ne fait que le gather).
***REMOVED*** Lib absente (vieille image) OU wrapper non applicable → repli numpy intégral : le repli EST
***REMOVED*** l'ancien code, octet-identique. getattr : un bobimxl d'ancienne image n'a pas mvk_available.
_MVK = bool(getattr(bobimxl, "mvk_available", lambda: False)())


***REMOVED*** ─── Latence (rolling avg, péremption 2 s) ──────────────────
class RollingMs:
    def __init__(self, n=30):
        self.d = deque(maxlen=n); self.last_ns = 0
    def push(self, ms_value):
        self.d.append(ms_value); self.last_ns = time.time_ns()
    def avg(self):
        if not self.d: return None
        if time.time_ns() - self.last_ns > 2_000_000_000: return None
        return round(sum(self.d) / len(self.d), 1)


***REMOVED*** ─── Layout YUV ─────────────────────────────────────────────
def _chroma_factors(chroma):
    cw = {{"420": 2, "422": 2, "444": 1}}.get(chroma, 2)
    ch = {{"420": 2, "422": 1, "444": 1}}.get(chroma, 1)
    return cw, ch

def _make_layout(w, h, chroma="422", bit_depth=8, fps_num=25, fps_den=1,
                 interlace_mode="progressive", frame_fps_num=None):
    """Dérivés de format d'un plan plein (source). Champ-natif : w/h = dims de GRAIN (champ si
    entrelacé) → on descale un CHAMP ; interlace_mode + frame_fps_num servent à déclarer les proxies
    entrelacés (passthrough). fps_num/den (cadence GRAIN) propagés ; frame_fps_num = cadence TRAME."""
    w -= w % 2; h -= h % 2
    deep = bit_depth >= 10
    bps  = 2 if deep else 1
    np_dt = np.uint16 if deep else np.uint8
    cw, ch = _chroma_factors(chroma)
    uv_w = w // cw; uv_h = h // ch
    y_sz = w * h * bps; uv_sz = uv_w * uv_h * bps
    fr_sz = y_sz + 2 * uv_sz
    return dict(width=w, height=h, chroma=chroma, bit_depth=bit_depth,
                fps_num=int(fps_num), fps_den=int(fps_den),
                bps=bps, np_dt=np_dt, cw=cw, ch=ch, uv_w=uv_w, uv_h=uv_h,
                y_sz=y_sz, uv_sz=uv_sz, fr_sz=fr_sz,
                interlace_mode=interlace_mode, frame_fps_num=int(frame_fps_num or fps_num))

def _proxy_dims(w, h, level, cw, ch):
    """Dimensions du proxy au niveau `level` : division entière puis alignement chroma/2.
    MÊME formule côté orchestrateur (app/plugins.derive_wiring) → tailles cohérentes."""
    pw = max(2, w // level); pw -= pw % max(2, cw)
    ph = max(2, h // level); ph -= ph % max(2, ch)
    return pw, ph

***REMOVED*** (format détecté via le flow_def MXL du producteur — cf. worker/reader.format())


***REMOVED*** ─── Redimensionnement (strided si ratio entier, sinon gather) ──
def resize_plane(plane, target_h, target_w):
    from_h, from_w = plane.shape
    if from_h == target_h and from_w == target_w:
        return plane
    if from_h % target_h == 0 and from_w % target_w == 0:
        return plane[::from_h // target_h, ::from_w // target_w]
    row_idx = (np.arange(target_h) * from_h / target_h).astype(int)
    col_idx = (np.arange(target_w) * from_w / target_w).astype(int)
    return plane[np.ix_(row_idx, col_idx)]


def _mvk_place_plane(dstv, plane, th, tw):
    """resize_plane + assignation FUSIONNÉS (mvk_place_into : plan source → vue destination en
    1 passe, plus de tableau intermédiaire). Indices nearest = MÊMES formules que resize_plane
    (division entière stridée si ratio entier, sinon troncature float), calculées ici — le C ne
    fait que le gather. False → repli resize_plane (bit-exact). Cf. multiview._mvk_place_plane
    (modèle de référence)."""
    if not _MVK or th <= 0 or tw <= 0:
        return False
    fh, fw = plane.shape
    if fh % th == 0 and fw % tw == 0:
        ri = (np.arange(th) * (fh // th)).astype(np.int32)
        return bobimxl.mvk_place_into(dstv, plane, ri, col0=0, col_step=fw // tw)
    ri = (np.arange(th) * fh / th).astype(np.int32)
    ci = (np.arange(tw) * fw / tw).astype(np.int32)
    return bobimxl.mvk_place_into(dstv, plane, ri, col_idx=ci)


def _proxy_slice_h(ph):
    """MODE TRANCHE — slice_height d'un proxy de hauteur ph : le plus petit diviseur sh de ph
    avec sh ≥ max(1, ph // _SLICE_TARGET) (~30 tranches ; 540→18, 270→9, 135→5). Aucun diviseur
    raisonnable (sh > ph//2, ex. hauteur première) → 0 = whole-frame (les consommateurs
    s'adaptent via gi.totalSlices, pas besoin d'un nombre de tranches homogène entre proxies)."""
    lo = max(1, ph // _SLICE_TARGET)
    for sh in range(lo, ph // 2 + 1):
        if ph % sh == 0:
            return sh
    return 0


def _mvk_band(out, y0, u0, v0, lyt, a, b, qa, qb):
    """Placement de bande FUSIONNÉ (mvk_place_into : plan source → vue du grain proxy en 1 passe,
    plus de tableau intermédiaire). Indices SOURCE ABSOLUS = MÊMES formules que le repli numpy de
    _emit_band (bit-exact) ; le C ne fait que le gather. False → repli intégral à l'appelant (un
    échec partiel — ex. Y posé, chroma refusé — fait ré-écrire toute la bande par le repli : sûr,
    cf. multiview._mvk_band, modèle de référence)."""
    pd = out[0]; py = out[2]; pu = out[3]; pv = out[4]
    src_h = lyt["height"]; s_uvh = lyt["uv_h"]
    if pd["strided"]:
        sy = src_h // pd["ph"]; sx = lyt["width"] // pd["pw"]
        ri = (np.arange(a, b) * sy).astype(np.int32)
        if not bobimxl.mvk_place_into(py[a:b], y0, ri, col0=0, col_step=sx):
            return False
        if qb > qa:
            scy = s_uvh // pd["uh"]; scx = lyt["uv_w"] // pd["uw"]
            rc = (np.arange(qa, qb) * scy).astype(np.int32)
            return bool(
                bobimxl.mvk_place_into(pu[qa:qb], u0, rc, col0=0, col_step=scx)
                and bobimxl.mvk_place_into(pv[qa:qb], v0, rc, col0=0, col_step=scx))
        return True
    ry = ((np.arange(a, b) * src_h) // pd["ph"]).astype(np.int32)
    if not bobimxl.mvk_place_into(py[a:b], y0, ry, col_idx=pd["cx"]):
        return False
    if qb > qa:
        rc = ((np.arange(qa, qb) * s_uvh) // pd["uh"]).astype(np.int32)
        return bool(
            bobimxl.mvk_place_into(pu[qa:qb], u0, rc, col_idx=pd["ccx"])
            and bobimxl.mvk_place_into(pv[qa:qb], v0, rc, col_idx=pd["ccx"]))
    return True


_vent = [None]   ***REMOVED*** (t_open0_ns, cumul_emit_ns, cumul_commit_ns) du dernier tour tranché
***REMOVED*** ★ SUCCÈS/ÉCHEC DU NOYAU FUSIONNÉ, COMPTÉ (2026-08-11). `_mvk_band` peut rendre False — donc
***REMOVED*** faire retomber la bande sur le repli numpy — SANS RIEN DIRE. Le précédent est documenté sur ce
***REMOVED*** même noyau côté mur (« mvk : False en silence ») et il a déjà coûté une enquête. Ici la question
***REMOVED*** est directe : l'écriture des bandes coûte 7,3 à 9,1 ms QUELLE QUE SOIT la granularité (30 bandes
***REMOVED*** ou 2 — mesuré), alors que le chemin pleine trame tient en 2,7 ms. Si le noyau refuse à chaque
***REMOVED*** appel, c'est numpy qui travaille et l'explication est là. On compte au lieu de supposer.
_mvk_stat = {{"ok": 0, "ko": 0}}
***REMOVED*** Écarter le noyau fusionné sur les proxies à RATIO ENTIER (copie stridée numpy préférée).
_v = CONFIG.get("mvk_skip_strided", True)
MVK_SKIP_STRIDED = _v if isinstance(_v, bool) else str(_v).strip().lower() in ("1","true","yes","on")


def _emit_band(out, y0, u0, v0, lyt, upto):
    """MODE TRANCHE — écrit dans le grain proxy le DELTA de lignes [déjà_écrites, upto),
    arrondi au multiple de lignes chroma entières (out[0]["chp"] = ph // uv_h ; trivial en 422).
    MÊME mapping nearest que resize_plane : fast-path vue STRIDÉE (ratio entier, simple
    slice-assign) sinon gather CHAÎNÉ lignes→colonnes — JAMAIS np.ix_ 2D (~120 µs/appel mesuré
    côté multiview, prohibitif à bande×proxy). Tenté d'abord via le kernel fusionné mvk
    (_mvk_band, bit-exact), repli numpy intégral sinon. Met à jour out[5] (lignes écrites, cumul)."""
    pd = out[0]; py = out[2]; pu = out[3]; pv = out[4]
    b = min(pd["ph"], int(upto)); b -= b % pd["chp"]
    a = out[5]
    if b <= a:
        return
    src_h = lyt["height"]; s_uvh = lyt["uv_h"]
    qa = a // pd["chp"]; qb = b // pd["chp"]
    ***REMOVED*** ★ RATIO ENTIER : la copie STRIDÉE numpy bat le gather fusionné (2026-08-11).
    ***REMOVED*** Le noyau mvk réussit toujours (mvk_ko = 0 sur 794 854 appels — vérifié, ce n'est pas un
    ***REMOVED*** repli silencieux), mais pour un proxy à ratio entier il fait un GATHER PAR INDICES de
    ***REMOVED*** lignes là où numpy fait une simple copie stridée `y0[a*sy:b*sy:sy, ::sx]` — un memcpy à
    ***REMOVED*** pas constant. On remplaçait donc une copie par un gather, en C mais un gather quand même.
    ***REMOVED*** Réglable pour pouvoir comparer A/B au banc ; false = comportement historique.
    if _MVK and not (pd["strided"] and MVK_SKIP_STRIDED):
        if _mvk_band(out, y0, u0, v0, lyt, a, b, qa, qb):
            _mvk_stat["ok"] += 1
            out[5] = b
            return
        _mvk_stat["ko"] += 1
    if pd["strided"]:
        sy = src_h // pd["ph"]; sx = lyt["width"] // pd["pw"]
        py[a:b] = y0[a * sy:b * sy:sy, ::sx]
        if qb > qa:
            scy = s_uvh // pd["uh"]; scx = lyt["uv_w"] // pd["uw"]
            pu[qa:qb] = u0[qa * scy:qb * scy:scy, ::scx]
            pv[qa:qb] = v0[qa * scy:qb * scy:scy, ::scx]
    else:
        ry = (np.arange(a, b) * src_h) // pd["ph"]
        py[a:b] = y0[ry][:, pd["cx"]]
        if qb > qa:
            rc = (np.arange(qa, qb) * s_uvh) // pd["uh"]
            pu[qa:qb] = u0[rc][:, pd["ccx"]]
            pv[qa:qb] = v0[rc][:, pd["ccx"]]
    out[5] = b


***REMOVED*** ─── État runtime ───────────────────────────────────────────
state_lock = threading.Lock()
***REMOVED*** inputs[slot] = {{"shm": name|None, "fmt": dict|None}}
state = {{"inputs": {{}}}}
for _i in range(N_INPUTS):
    _shm = (CONFIG.get("input_%d" % (_i + 1)) or None)
    _fmt = CONFIG.get("input_%d_fmt" % (_i + 1))
    state["inputs"][_i] = {{"shm": (_shm or None), "fmt": _fmt if isinstance(_fmt, dict) else None}}

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "inputs_latency_ms": {{}},
           "own_latency_ms": None, "sources": {{}}, "plugin_version": PLUGIN_VERSION,
           ***REMOVED*** Ventilation du tour en MODE TRANCHE (cf. `_vent`) : ouverture des grains de
           ***REMOVED*** sortie / écriture des bandes / commits progressifs. None hors mode tranche.
           "slice_breakdown_ms": None,
           "mvk": _MVK}}   ***REMOVED*** kernel compose fusionné C actif (chemin CPU) — statique, cf. _MVK

bus_error = threading.Event()
def _handle_sigbus(signum, frame):
    log("SIGBUS reçu — réouverture Reader/Writer MXL", "warning")
    bus_error.set()
signal.signal(signal.SIGBUS, _handle_sigbus)


def _proxy_name(src, level):
    return f"{{src}}__p{{level}}"

def _extra_name(src, w, h):
    return f"{{src}}__s{{w}}x{{h}}"

def _align_extra(rw, rh, src_w, src_h, cw, ch):
    """Dimensions effectives d'un proxy sur-mesure : jamais d'upscale (clamp à la source),
    alignées chroma/2. MÊME formule que app/plugins.derive_wiring → nom/dims cohérents."""
    pw = min(max(2, int(rw)), src_w); pw -= pw % max(2, cw)
    ph = min(max(2, int(rh)), src_h); ph -= ph % max(2, ch)
    return pw, ph

def _produced_proxies():
    """Liste plate des proxies actuellement produits (pour /state) : octaves + sur-mesure."""
    with state_lock:
        srcs = [(s["shm"]) for s in state["inputs"].values() if s["shm"]]
    out = []
    for s in srcs:
        out.extend(_proxy_name(s, L) for L in LEVELS)
        for wh in _extra_for(s):
            try: out.append(_extra_name(s, int(wh[0]), int(wh[1])))
            except (TypeError, ValueError, IndexError): pass
    return out


***REMOVED*** ─── HTTP : metrics 8080 + control 8082 ─────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock: payload = dict(metrics)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
    def log_message(self, *a): pass


class ControlHandler(BaseHTTPRequestHandler):
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n: return {{}}
        try: return json.loads(self.rfile.read(n).decode())
        except Exception: return {{}}

    def _reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path == "/state":
            with state_lock:
                inputs = {{str(k): v["shm"] for k, v in state["inputs"].items()}}
            self._reply(200, {{
                "inputs": inputs,
                "levels": LEVELS,
                "ring": OUT_RING,
                "proxies": _produced_proxies(),
                "plugin_version": PLUGIN_VERSION,
                "log_level": LOG_LEVEL,   ***REMOVED*** lisible en condition de macro
                "mxl_lib": _mxl_lib_state(),
            }})
        else:
            self._reply(404, {{"error": "not found"}})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/input":
            try: slot = int(body.get("slot", body.get("idx")))
            except (TypeError, ValueError): slot = None
            shm = (body.get("shm") or "").strip() or None
            fmt = body.get("format")
            if slot is None or not (0 <= slot < N_INPUTS):
                self._reply(400, {{"error": "slot hors plage"}}); return
            with state_lock:
                cur = state["inputs"].setdefault(slot, {{"shm": None, "fmt": None}})
                cur["shm"] = shm
                if fmt and fmt.get("width") and fmt.get("height"):
                    cur["fmt"] = fmt
                elif shm is None:
                    cur["fmt"] = None
            self._reply(200, {{"ok": True}})
        elif self.path == "/extra_sizes":
            ***REMOVED*** Hot-apply des tailles sur-mesure : {{"sizes": {{"<src>": [[w,h], …]}}}}. Remplace
            ***REMOVED*** l'ensemble et bump la génération → les workers re-synchronisent (ajout/retrait ciblé,
            ***REMOVED*** sans coupure des proxies inchangés). Aucun redéploiement.
            sizes = body.get("sizes")
            if not isinstance(sizes, dict):
                self._reply(400, {{"error": "sizes manquant/invalide"}}); return
            clean = {{}}
            for src, lst in sizes.items():
                acc = []
                for wh in (lst or []):
                    try: acc.append([int(wh[0]), int(wh[1])])
                    except (TypeError, ValueError, IndexError): pass
                if acc:
                    clean[str(src)] = acc
            with extra_lock:
                _EXTRA.clear(); _EXTRA.update(clean)
                _extra_gen[0] += 1
            self._reply(200, {{"ok": True, "sources": len(clean)}})
        elif self.path == "/log_level":
            ***REMOVED*** Verbosité À CHAUD (pas de redéploiement). Le niveau PERSISTANT reste le champ
            ***REMOVED*** `log_level` du config_schema ; celui-ci est volatil (perdu au redéploiement).
            ok = set_log_level(body.get("level") or body.get("log_level"))
            self._reply(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}})

        elif self.path == "/reset":
            with state_lock:
                for k in state["inputs"]:
                    state["inputs"][k] = {{"shm": None, "fmt": None}}
            self._reply(200, {{"ok": True}})
        else:
            self._reply(404, {{"error": "not found"}})

    def log_message(self, *a): pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(),
                 daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(),
                 daemon=True).start()


***REMOVED*** ─── Worker par source ──────────────────────────────────────
def _desired_specs(src, lyt):
    """Ensemble VOULU de proxies pour une source : octaves LEVELS + tailles sur-mesure (à chaud).
    Renvoie une LISTE de (name, pw, ph) dédoublonnée (une taille sur-mesure = octave est ignorée)."""
    cw = lyt["cw"]; ch = lyt["ch"]; W = lyt["width"]; H = lyt["height"]
    specs = []
    for L in LEVELS:
        pw, ph = _proxy_dims(W, H, L, cw, ch)
        specs.append((_proxy_name(src, L), pw, ph))
    seen = set((pw, ph) for _, pw, ph in specs)
    for wh in _extra_for(src):
        try:
            pw, ph = _align_extra(wh[0], wh[1], W, H, cw, ch)
        except (TypeError, ValueError, IndexError):
            continue
        if pw < 2 or ph < 2 or (pw, ph) in seen:
            continue
        seen.add((pw, ph))
        specs.append((_extra_name(src, pw, ph), pw, ph))
    return specs

def _make_proxy(name, lyt, pw, ph):
    cw = lyt["cw"]; ch = lyt["ch"]
    uw = pw // cw; uh = ph // ch
    ***REMOVED*** Champ-natif : le grain proxy = 1 CHAMP (ph lignes, descalé du champ source). On déclare la TRAME
    ***REMOVED*** (ph*2 + interlace_mode du producteur) → libmxl redonne des grains-champs de ph lignes ;
    ***REMOVED*** grain_rate = cadence TRAME. Progressif : frame_h=ph, interlace=progressive → inchangé.
    il = lyt.get("interlace_mode", "progressive")
    frame_h = ph * 2 if il.startswith("interlaced") else ph
    ***REMOVED*** MODE TRANCHE : slice_height PAR PROXY (progressif seulement — l'entrelacé reste
    ***REMOVED*** whole-frame). sh=0 → flowDef inchangé (aucun champ slice_height, historique octet-identique).
    sh = _proxy_slice_h(ph) if (SLICE_MODE and not il.startswith("interlaced")) else 0
    w = bobimxl.Writer(inst, name, pw, frame_h, lyt["chroma"], lyt["bit_depth"],
                       lyt.get("frame_fps_num", lyt["fps_num"]), lyt["fps_den"],
                       interlace=il,
                       **({{"slice_height": sh}} if sh else {{}}))
    ***REMOVED*** Pré-calculs bande (mode tranche) : fast-path vue stridée si ratio ENTIER (mêmes conditions
    ***REMOVED*** que resize_plane), sinon indices de COLONNES du gather chaîné, calculés UNE fois.
    strided = (lyt["height"] % ph == 0 and lyt["width"] % pw == 0)
    ***REMOVED*** cx/ccx en int32 (contrat mvk_place_into, cf. _mvk_band) ; identiques en valeur au fancy-
    ***REMOVED*** indexing int64 d'origine (mêmes petits entiers, aucune perte) → repli numpy inchangé.
    return dict(name=name, writer=w, pw=pw, ph=ph, uw=uw, uh=uh,
                sh=sh, chp=max(1, ph // max(1, uh)), strided=strided,
                cx=None if strided else ((np.arange(pw) * lyt["width"]) // pw).astype(np.int32),
                ccx=None if strided else ((np.arange(uw) * lyt["uv_w"]) // uw).astype(np.int32))

def _drop_proxy(d):
    """Retire un proxy : ferme le Writer (le flux MXL est récupéré au GC)."""
    try: d["writer"].close()
    except Exception: pass

def _sync_proxies(src, lyt, current):
    """Aligne la LISTE de proxies sur l'ensemble voulu (hot-apply) : GARDE les inchangés
    (Writer intact = aucune coupure), RETIRE (ferme) les obsolètes, OUVRE les ajoutés."""
    want = {{name: (pw, ph) for name, pw, ph in _desired_specs(src, lyt)}}
    keep = []
    for d in (current or []):
        if want.get(d["name"]) == (d["pw"], d["ph"]):
            keep.append(d); del want[d["name"]]
        else:
            _drop_proxy(d)
    for name, (pw, ph) in want.items():
        try:
            keep.append(_make_proxy(name, lyt, pw, ph))
        except Exception as _e:
            log(f"proxy {{name}} : création impossible : {{_e}}", "warning")
    if want:
        try: inst.garbage_collect()   ***REMOVED*** récupère les flux des proxies retirés
        except Exception: pass
    return keep

def _close_proxies(proxies):
    for d in (proxies or []):
        _drop_proxy(d)

***REMOVED*** Watchdog Reader périmé : une source qui n'avance plus depuis ce délai alors que le flux existe
***REMOVED*** toujours = shm probablement RECRÉÉ sous le même nom (NMOS re-souscrit, source coupée/re-câblée) →
***REMOVED*** notre mmap pointe le buffer mort. flow_id étant déterministe du nom, `wanted == in_name` ne
***REMOVED*** déclenche jamais le reopen → on ROUVRE le Reader pour se raccrocher au buffer frais (cf. multiview
***REMOVED*** 0.18.4). 0,5 s ≈ 25 trames à 50 fps : au-delà d'un simple drop/jitter, en-deçà d'une coupure visible.
_STALE_REOPEN_NS = 500_000_000

***REMOVED*** ── ÉCHEC D'OUVERTURE D'UNE ENTRÉE : tracé, PUBLIÉ, et espacé ────────────────────────────────
***REMOVED***
***REMOVED*** Incident Horace du 2026-08-19 : trois workers sur seize n'ont jamais réussi à ouvrir leur
***REMOVED*** source. Ils ont bouclé 43 HEURES à 20 Hz sans écrire une seule ligne au journal, et le seul
***REMOVED*** indice était une ABSENCE — leur slot manquait dans `/state.sources`. Pendant ce temps la
***REMOVED*** pyramide continuait d'ANNONCER ces proxys dans son état périodique, donc tout paraissait normal,
***REMOVED*** et les murs en aval tournaient à vide sur des proxys jamais écrits (cf. la boucle de
***REMOVED*** reconnexion du multiview, qui a fini par tuer un nœud). Famille : l'échec silencieux.
***REMOVED***
***REMOVED*** Trois exigences, et pas deux :
***REMOVED***   1. TRACER — mais sans rafale : les 3 premières tentatives puis une sur cent.
***REMOVED***   2. PUBLIER — `metrics["entrees_ko"]` rend l'échec LISIBLE de l'extérieur (:8080, page
***REMOVED***      Monitoring, alerte). Une absence ne se voit pas ; une entrée qui dit « je n'y arrive pas
***REMOVED***      depuis 43 h » se voit. C'est le point qui manquait vraiment.
***REMOVED***   3. ESPACER — 20 Hz sur une source absente ne sert à rien et occupe un cœur pour rien.
***REMOVED***      Palier de 50 ms à 2 s. Ne JAMAIS abandonner : une source peut revenir.
_ko_etat = {{}}    ***REMOVED*** slot → {{"n", "shm", "motif", "t0"}}


def _attente_echec(slot):
    """Palier exponentiel borné, à partir du nombre d'échecs consécutifs de CE slot."""
    n = (_ko_etat.get(slot) or {{}}).get("n", 1)
    return min(2.0, 0.05 * (2 ** min(n, 6)))


def _ouverture_ko(slot, shm, motif):
    st = _ko_etat.get(slot)
    if st is None or st.get("shm") != shm:
        st = {{"n": 0, "shm": shm, "motif": motif, "t0": time.time()}}
        _ko_etat[slot] = st
    st["n"] += 1
    st["motif"] = motif
    n = st["n"]
    if n <= 3 or n % 100 == 0:
        log(f"pyramide: entrée {{slot}} ({{shm}}) INEXPLOITABLE — {{motif}} "
            f"(tentative {{n}}, depuis {{int(time.time() - st['t0'])}} s)", "warning")
    with metrics_lock:
        metrics.setdefault("entrees_ko", {{}})[str(slot)] = {{
            "shm": shm, "motif": motif, "tentatives": n,
            "depuis_s": int(time.time() - st["t0"])}}


def _ouverture_ok(slot):
    st = _ko_etat.pop(slot, None)
    if st is not None:
        log(f"pyramide: entrée {{slot}} ({{st['shm']}}) rétablie après {{st['n']}} tentative(s) "
            f"et {{int(time.time() - st['t0'])}} s", "warning")
    with metrics_lock:
        _ko = metrics.get("entrees_ko")
        if _ko:
            _ko.pop(str(slot), None)


def worker(slot):
    _sd = _sd_new()
    _delai_etage[slot] = _sd      ***REMOVED*** visible du thread de métriques, propre à CE worker
    reader  = None
    in_name = None
    src_lyt = None        ***REMOVED*** layout source actif
    proxies = []          ***REMOVED*** liste de descripteurs de proxy
    my_gen  = -1          ***REMOVED*** génération extra_sizes synchronisée (hot-apply)
    last_idx = -1
    last_fresh_ns = time.time_ns()   ***REMOVED*** dernier instant où l'index source a AVANCÉ (watchdog stale)
    lat_in = RollingMs(); own = RollingMs()
    start = time.time(); produced = 0

    def _teardown():
        nonlocal reader, in_name, src_lyt, proxies, my_gen, last_idx, produced, start
        if reader is not None:
            try: reader.close()
            except Exception: pass
        reader = None; in_name = None; src_lyt = None
        _close_proxies(proxies); proxies = []; my_gen = -1
        last_idx = -1; produced = 0; start = time.time()

    while True:
        if bus_error.is_set():
            _teardown(); time.sleep(0.5)
            if bus_error.is_set(): bus_error.clear()
            continue

        with state_lock:
            cur = dict(state["inputs"].get(slot) or {{}})
        wanted = cur.get("shm")

        if not wanted:
            if in_name is not None:
                _teardown()
                with metrics_lock: metrics["sources"].pop(str(slot), None)
            time.sleep(0.1); continue

        ***REMOVED*** (Ré)ouverture sur changement de source → Reader MXL (lève si le flux pas encore là)
        if wanted != in_name:
            _teardown()
            try:
                reader = bobimxl.Reader(inst, wanted)
                in_name = wanted
                last_fresh_ns = time.time_ns()
                _ouverture_ok(slot)
            except Exception as e:
                _ouverture_ko(slot, wanted, "flux introuvable : %s" % e)
                time.sleep(_attente_echec(slot)); continue

        ***REMOVED*** Format LU DU flow_def du producteur (source de vérité côté donnée)
        f = reader.format() if reader is not None else None
        new_lyt = _make_layout(f["width"], f["height"], f["chroma"], f["bit_depth"],
                               f["fps_num"], f["fps_den"],
                               f.get("interlace_mode", "progressive"),
                               f.get("frame_fps_num")) if f else None
        if new_lyt is None:
            ***REMOVED*** Le flux existe mais son flow_def est illisible/incomplet. Même règle que ci-dessus :
            ***REMOVED*** un worker qui ne peut pas décider du format ne produira JAMAIS, il doit le dire.
            _ouverture_ko(slot, wanted, "format illisible (flow_def absent ou incomplet)")
            time.sleep(_attente_echec(slot)); continue
        _ouverture_ok(slot)

        if src_lyt is None or (src_lyt["width"], src_lyt["height"], src_lyt["chroma"],
                               src_lyt["bit_depth"]) != (new_lyt["width"], new_lyt["height"],
                                                          new_lyt["chroma"], new_lyt["bit_depth"]):
            _close_proxies(proxies); proxies = []
            src_lyt = new_lyt; my_gen = -1
            last_idx = -1; produced = 0; start = time.time()
            log(f"slot {{slot}} {{wanted}} : {{src_lyt['width']}}x{{src_lyt['height']}} "
                f"{{src_lyt['chroma']}}", "info")

        ***REMOVED*** Hot-apply : (re)synchronise les proxies si l'ensemble voulu a changé (extra_sizes), SANS
        ***REMOVED*** toucher aux proxies inchangés (aucune coupure pour les consommateurs en cours).
        if my_gen != _extra_gen[0]:
            proxies = _sync_proxies(wanted, src_lyt, proxies)
            my_gen = _extra_gen[0]
            log(f"slot {{slot}} {{wanted}} → proxies {{[p['name'] for p in proxies]}}", "info")

        ***REMOVED*** MODE TRANCHE : uniquement si la SOURCE est progressive (l'entrelacé garde le chemin
        ***REMOVED*** historique grain-complet ; ses proxies restent whole-frame, cf. _make_proxy).
        _slice_src = SLICE_MODE and not str(src_lyt.get("interlace_mode")
                                            or "progressive").startswith("interlaced")

        if _slice_src:
            ***REMOVED*** ─── MODE TRANCHE : suivre le grain de TÊTE (peut être EN COURS d'écriture) ───
            h = reader.head_index()
            if h == bobimxl.MXL_UNDEFINED_INDEX or h == last_idx:
                ***REMOVED*** Watchdog : source figée trop longtemps → rouvrir le Reader (shm recréé sous
                ***REMOVED*** le même nom) — même logique que le chemin historique, bornée à 1×/_STALE_REOPEN_NS.
                if (time.time_ns() - last_fresh_ns) > _STALE_REOPEN_NS:
                    try: reader.close()
                    except Exception: pass
                    ***REMOVED*** GC ENTRE close et reopen (parade générique du piège des générations, cf.
                    ***REMOVED*** moteur tx_reopen_if_stale) : sans GC le flux périmé reste résolvable par
                    ***REMOVED*** nom et le reopen retombe sur L'ORPHELIN → gel permanent (mesuré : pyramide
                    ***REMOVED*** figée 40 min après recréation de sa source, tout l'aval sans données).
                    try: inst.garbage_collect()
                    except Exception: pass
                    try:
                        reader = bobimxl.Reader(inst, in_name)
                    except Exception:
                        reader = None; in_name = None
                    last_fresh_ns = time.time_ns()
                    if reader is None:
                        time.sleep(0.05)
                time.sleep(0.002); continue
            ***REMOVED*** 1ʳᵉ tranche du grain de tête ; pas encore là (tête à peine réclamée) ou flux sans
            ***REMOVED*** le patch slices → repli get_latest (grain complet, boucle dégénérée sans attente).
            got = reader.get_slice(h, 1, timeout_ns=2_000_000)
            if got is None:
                got = reader.get_latest()
            if got is None:
                time.sleep(0.005); continue
            idx = got[0]
            if idx == last_idx:      ***REMOVED*** repli retombé sur le grain déjà traité (h-1)
                time.sleep(0.002); continue
            last_fresh_ns = time.time_ns()
            gi_s = got[1]

            try:
                ***REMOVED*** Vues ZÉRO-COPIE sur le payload (PAS de bytes() copie : on ne lit jamais
                ***REMOVED*** au-delà des tranches valides — attente ciblée plus bas ; le handler SIGBUS
                ***REMOVED*** couvre la recréation du flux amont, comme pour le chemin historique).
                arr = got[2][:src_lyt["fr_sz"]].view(src_lyt["np_dt"])
                ny = src_lyt["width"] * src_lyt["height"]
                nu = src_lyt["uv_w"] * src_lyt["uv_h"]
                y0 = arr[:ny].reshape(src_lyt["height"], src_lyt["width"])
                u0 = arr[ny:ny + nu].reshape(src_lyt["uv_h"], src_lyt["uv_w"])
                v0 = arr[ny + nu:ny + 2 * nu].reshape(src_lyt["uv_h"], src_lyt["uv_w"])
            except Exception:
                last_idx = idx; time.sleep(0.002); continue

            read_ns = time.time_ns()
            src_h = src_lyt["height"]; s_ch = src_lyt["ch"]; s_uvh = src_lyt["uv_h"]
            bps = src_lyt["bps"]; np_dt = src_lyt["np_dt"]
            total = max(1, int(gi_s.totalSlices or 1))
            islh  = max(1, src_h // total)   ***REMOVED*** lignes source par tranche (tranches égales)
            ***REMOVED*** Grains de TOUS les proxies ouverts à l'index SOURCE (propagation d'index
            ***REMOVED*** inchangée) + vues par plan — offsets Y|Cb|Cr en OCTETS (bps = octets/échantillon,
            ***REMOVED*** largeur chroma uv_w), .view(np_dt) pour écrire en échantillons.
            ***REMOVED*** ★ VENTILATION DU TOUR (2026-08-11). Le mode tranche coûte 15,5 à 18,7 ms de temps
            ***REMOVED*** propre contre 2,7 en image entière — un facteur 6,6 sur un étage qui ne fait que
            ***REMOVED*** réduire des images, et RIEN ne disait où il passe. Le mur a `compose_breakdown_ms`
            ***REMOVED*** depuis cette nuit et c'est ce qui a permis de le corriger ; la pyramide n'avait que
            ***REMOVED*** son total. Trois postes, sur la MÊME trame : ouverture des grains de sortie, écriture
            ***REMOVED*** des bandes (`_emit_band`, tous proxies), et commits progressifs.
            _t_open0 = time.time_ns()
            _t_emit = 0; _t_commit = 0
            outs = []   ***REMOVED*** [pd, gi_p, vue_y, vue_u, vue_v, lignes_écrites, k_commité]
            for pd in proxies:
                _gx, gi_p, vw_p = pd["writer"].open_grain(src_index=idx)
                ***REMOVED*** ÉCART D'INDEX écrit − source (diagnostic 2026-08-11). `open_grain(src_index=)`
                ***REMOVED*** est censé PROPAGER la coordonnée du grain source ; mais `next_index` applique
                ***REMOVED*** `max(candidat, _counter + 1)`, donc un writer qui a pris de l'avance UNE fois ne
                ***REMOVED*** peut plus jamais redescendre sur sa source — la propagation devient à sens
                ***REMOVED*** unique et l'écart accumulé est PERMANENT. Un consommateur lisant la tête reçoit
                ***REMOVED*** alors du contenu vieux de cet écart, sans qu'aucun compteur de latence ne le
                ***REMOVED*** voie (le grain est écrit à l'heure, c'est son CONTENU qui est vieux).
                _ecart_idx[pd["name"]] = int(_gx) - int(idx)
                pys = pd["pw"] * pd["ph"] * bps
                puv = pd["uw"] * pd["uh"] * bps
                outs.append([pd, gi_p,
                             vw_p[:pys].view(np_dt).reshape(pd["ph"], pd["pw"]),
                             vw_p[pys:pys + puv].view(np_dt).reshape(pd["uh"], pd["uw"]),
                             vw_p[pys + puv:pys + 2 * puv].view(np_dt).reshape(pd["uh"], pd["uw"]),
                             0, 0])
            if proxies:
                _pire = max(proxies, key=lambda q: _ecart_idx.get(q["name"], 0))
                _sd.observe(_pire["writer"],
                            int(idx) + int(_ecart_idx.get(_pire["name"], 0)),
                            [(reader, idx)], propage=True)
            ***REMOVED*** Budget d'attente TOTAL ≈ 1,5 période de trame : une source en retard ne bloque
            ***REMOVED*** jamais les proxies au-delà d'une demi-trame après le nominal.
            deadl_ns = time.monotonic_ns() + int(1.5e9 * src_lyt["fps_den"]
                                                 / max(1, src_lyt["fps_num"]))
            valid = max(1, int(gi_s.validSlices or 1))
            wait_ns = 0   ***REMOVED*** cumul des ATTENTES get_slice — exclues de own/latency_ms (le cap
                          ***REMOVED*** réactif de l'orchestrateur y lit la SATURATION du worker, pas le
                          ***REMOVED*** suivi du fil ; sinon lat≈période → délestage de proxies à tort)
            for j in range(1, total + 1):
                if j > valid:
                    left = deadl_ns - time.monotonic_ns()
                    _w0 = time.monotonic_ns()
                    g = (reader.get_slice(idx, j, timeout_ns=max(1, left))
                         if left > 0 else None)
                    wait_ns += time.monotonic_ns() - _w0
                    if g is not None:
                        valid = max(j, int(g[1].validSlices or j))
                    else:
                        ***REMOVED*** Budget épuisé / producteur en retard → REPLI : compléter TOUS les
                        ***REMOVED*** proxies avec le reste du DERNIER grain COMPLET (idx-1) si disponible
                        ***REMOVED*** (léger tearing d'UNE image), sinon les lignes déjà écrites restent ;
                        ***REMOVED*** commit FINAL dans tous les cas (un grain laissé partiel ne serait
                        ***REMOVED*** jamais lisible par un consommateur whole-frame) puis on sort.
                        gp = reader.get(idx - 1, timeout_ns=2_000_000) if idx > 0 else None
                        if gp is not None:
                            try:
                                arrp = gp[2][:src_lyt["fr_sz"]].view(np_dt)
                                y0 = arrp[:ny].reshape(src_h, src_lyt["width"])
                                u0 = arrp[ny:ny + nu].reshape(s_uvh, src_lyt["uv_w"])
                                v0 = arrp[ny + nu:ny + 2 * nu].reshape(s_uvh, src_lyt["uv_w"])
                            except Exception:
                                gp = None
                        for out in outs:
                            if gp is not None:
                                _emit_band(out, y0, u0, v0, src_lyt, out[0]["ph"])
                            out[0]["writer"].commit(out[1], valid_slices=None)
                        break
                ***REMOVED*** Tranche source j dispo : lignes [0, sr) valides sur les 3 plans → pour chaque
                ***REMOVED*** proxy, n'écrire QUE le delta de lignes désormais calculables (dernière ligne
                ***REMOVED*** source requise < sr, même mapping nearest que resize_plane), borné par les
                ***REMOVED*** lignes CHROMA source garanties (sc = sr // s_ch, plancher conservateur).
                sr = min(src_h, j * islh)
                sc = sr // s_ch
                for out in outs:
                    pd = out[0]
                    n_y = min(pd["ph"], (sr * pd["ph"] + src_h - 1) // src_h)
                    n_c = min(pd["uh"], (sc * pd["uh"] + s_uvh - 1) // s_uvh)
                    _e0 = time.time_ns()
                    _emit_band(out, y0, u0, v0, src_lyt, min(n_y, n_c * pd["chp"]))
                    _t_emit += time.time_ns() - _e0
                    _c0 = time.time_ns()
                    if j == total:
                        ***REMOVED*** Commit FINAL : validSlices=totalSlices → grain complet publié.
                        pd["writer"].commit(out[1], valid_slices=None)
                    elif pd["sh"]:
                        k = out[5] // pd["sh"]
                        if k > out[6]:   ***REMOVED*** commit progressif (réveille l'aval), jamais en arrière
                            out[6] = k
                            pd["writer"].commit(out[1], valid_slices=k)
                    _t_commit += time.time_ns() - _c0
            out_ns = time.time_ns()
            _vent[0] = (_t_open0, _t_emit, _t_commit)
        else:
            ***REMOVED*** ─── Verrou 1:1 : attendre un NOUVEAU grain source (chemin historique) ───
            got = reader.get_latest()
            if got is None:
                time.sleep(0.005); continue
            idx = got[0]
            if idx == last_idx:
                ***REMOVED*** Watchdog : source figée trop longtemps → rouvrir le Reader (shm recréé sous le
                ***REMOVED*** même nom). On re-arme last_fresh_ns pour ne re-tenter qu'au plus tous les
                ***REMOVED*** _STALE_REOPEN_NS (pas de thrash si la source est réellement morte ; reopen
                ***REMOVED*** inoffensif sinon).
                if (time.time_ns() - last_fresh_ns) > _STALE_REOPEN_NS:
                    try: reader.close()
                    except Exception: pass
                    ***REMOVED*** GC ENTRE close et reopen (parade générique du piège des générations, cf.
                    ***REMOVED*** moteur tx_reopen_if_stale) : sans GC le flux périmé reste résolvable par
                    ***REMOVED*** nom et le reopen retombe sur L'ORPHELIN → gel permanent (mesuré : pyramide
                    ***REMOVED*** figée 40 min après recréation de sa source, tout l'aval sans données).
                    try: inst.garbage_collect()
                    except Exception: pass
                    try:
                        reader = bobimxl.Reader(inst, in_name)
                    except Exception:
                        reader = None; in_name = None
                    last_fresh_ns = time.time_ns()
                    if reader is None:
                        time.sleep(0.05)
                time.sleep(0.002); continue
            last_fresh_ns = time.time_ns()

            try:
                ***REMOVED*** ZÉRO-COPIE, comme le MODE TRANCHE ci-dessus et comme le multiview sur les MÊMES
                ***REMOVED*** flux. Ce chemin faisait `np.frombuffer(bytes(...))` : une copie de la trame
                ***REMOVED*** ENTIÈRE à chaque grain, avant tout descale. Mesuré au banc (2026-07-29,
                ***REMOVED*** 8 sources 1080p50) : 1,24 Go/s d'allocation+copie+libération, ~300 000 défauts de
                ***REMOVED*** page/s — soit 115,8 % d'un cœur consommés AVANT de produire le moindre proxy,
                ***REMOVED*** contre 4,0 % pour produire un proxy. Les trois quarts du coût de la pyramide
                ***REMOVED*** étaient cette copie. `get_latest` et `get_slice` renvoient la MÊME vue `_np_view`
                ***REMOVED*** dans le shm (cf. bobimxl) : la copie n'apportait aucune garantie que le chemin
                ***REMOVED*** tranche n'ait déjà, et le handler SIGBUS couvre la recréation du flux amont pour
                ***REMOVED*** les deux. Legacy jamais repassé quand le mode tranche est arrivé.
                arr = got[2][:src_lyt["fr_sz"]].view(src_lyt["np_dt"])
                ny = src_lyt["width"] * src_lyt["height"]
                nu = src_lyt["uv_w"] * src_lyt["uv_h"]
                y0 = arr[:ny].reshape(src_lyt["height"], src_lyt["width"])
                u0 = arr[ny:ny + nu].reshape(src_lyt["uv_h"], src_lyt["uv_w"])
                v0 = arr[ny + nu:ny + 2 * nu].reshape(src_lyt["uv_h"], src_lyt["uv_w"])
            except Exception:
                last_idx = idx; time.sleep(0.002); continue

            read_ns = time.time_ns()
            ***REMOVED*** ─── Descale + écriture de chaque proxy (octaves + sur-mesure), index source propagé ───
            ***REMOVED*** Tenté d'abord via le kernel fusionné mvk (resize+écriture DIRECTE dans la vue du
            ***REMOVED*** grain, plus de tableau intermédiaire ni de tobytes()) ; repli numpy intégral sinon
            ***REMOVED*** (un échec partiel — ex. Y posé, chroma refusé — fait ré-écrire toute la trame par le
            ***REMOVED*** repli : sûr, mêmes octets qu'avant recalculés dans les mêmes vues).
            bps = src_lyt["bps"]; np_dt = src_lyt["np_dt"]
            for pd in proxies:
                _gi_idx, gi_p, vw_p = pd["writer"].open_grain(src_index=idx)
                pys = pd["pw"] * pd["ph"] * bps
                puv = pd["uw"] * pd["uh"] * bps
                py_v = vw_p[:pys].view(np_dt).reshape(pd["ph"], pd["pw"])
                pu_v = vw_p[pys:pys + puv].view(np_dt).reshape(pd["uh"], pd["uw"])
                pv_v = vw_p[pys + puv:pys + 2 * puv].view(np_dt).reshape(pd["uh"], pd["uw"])
                if not (_MVK
                        and _mvk_place_plane(py_v, y0, pd["ph"], pd["pw"])
                        and _mvk_place_plane(pu_v, u0, pd["uh"], pd["uw"])
                        and _mvk_place_plane(pv_v, v0, pd["uh"], pd["uw"])):
                    py_v[...] = resize_plane(y0, pd["ph"], pd["pw"])
                    pu_v[...] = resize_plane(u0, pd["uh"], pd["uw"])
                    pv_v[...] = resize_plane(v0, pd["uh"], pd["uw"])
                pd["writer"].commit(gi_p)
            out_ns = time.time_ns()

        last_idx = idx
        produced += 1
        ***REMOVED*** Point sûr GC par worker (cf. bloc gc.disable() en tête) : le dernier proxy du grain est
        ***REMOVED*** committé, temps mort avant le grain source suivant. gen0+gen1 seulement (sub-ms) — le
        ***REMOVED*** gen2 cadencé vit dans la boucle d'agrégation.
        gc.collect(1)
        lat_in.push((bobimxl.now_tai() - reader.last_write_time()) / 1e6)
        ***REMOVED*** own = durée de TRAVAIL de production du grain proxy. En MODE TRANCHE les attentes
        ***REMOVED*** get_slice (suivi du fil, ≈ période de trame) sont EXCLUES (wait_ns) : le cap réactif
        ***REMOVED*** de l'orchestrateur (reconcile_pyramide_sizes, _LAT_HI_FRAC) lit ici la SATURATION du
        ***REMOVED*** worker vs son budget de trame — compter le suivi déclencherait un délestage à tort.
        own.push((out_ns - read_ns - (wait_ns if _slice_src else 0)) / 1e6)

        if produced % 25 == 0:
            with metrics_lock:
                ***REMOVED*** MAJ en place (sans écraser `fps`, calculé par delta de frame_index dans la boucle
                ***REMOVED*** d'agrégation = fenêtre glissante 1 s, reflète le débit réel et tombe à 0 si figé).
                s = metrics["sources"].setdefault(str(slot), {{}})
                s["shm"] = wanted
                s["frame_index"] = idx
                s["latency_ms"] = own.avg()
                s["proxies"] = [{{"name": pd["name"], "w": pd["pw"], "h": pd["ph"],
                                 "kind": ("custom" if "__s" in pd["name"] else "oct")}}
                                for pd in proxies]
                if wanted:
                    metrics["inputs_latency_ms"][wanted] = lat_in.avg()


***REMOVED*** ─── Boucle d'agrégation des métriques globales ─────────────
threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(N_INPUTS)]
for t in threads:
    t.start()

_fps_prev = {{}}   ***REMOVED*** slot → (frame_index, t) pour le fps en fenêtre glissante (1 s)
_gc_ticks = 0
_gc_max_full_ms = 0.0
while True:
    time.sleep(1.0)
    now = time.time()
    ***REMOVED*** gen2 cadencé (~5 s) et MESURÉ (cf. bloc gc.disable() en tête — ~0,1 ms grâce au freeze ;
    ***REMOVED*** recette : gc_full_ms doit rester à quelques ms, sinon investiguer la croissance du tas).
    _gc_ticks += 1
    if _gc_ticks % 5 == 0:
        _t_gc = time.monotonic_ns()
        gc.collect(2)
        _gc_full_ms = (time.monotonic_ns() - _t_gc) / 1e6
        if _gc_full_ms > _gc_max_full_ms:
            _gc_max_full_ms = _gc_full_ms
        with metrics_lock:
            metrics["gc_full_ms"] = {{"last": round(_gc_full_ms, 2),
                                     "max": round(_gc_max_full_ms, 2)}}
    with metrics_lock:
        srcs = metrics["sources"]
        total_fps = 0.0
        for slot, s in srcs.items():
            fi = s.get("frame_index") or 0
            pf, pt = _fps_prev.get(slot, (fi, now - 1.0))
            dt = now - pt
            fps = round((fi - pf) / dt, 1) if dt > 0 and fi >= pf else 0.0
            s["fps"] = max(0.0, fps)
            _fps_prev[slot] = (fi, now)
            total_fps += s["fps"]
        owns = [s.get("latency_ms") for s in srcs.values() if s.get("latency_ms") is not None]
        metrics["fps"] = round(total_fps, 1)
        metrics["frame_index"] = max((s.get("frame_index") or 0) for s in srcs.values()) if srcs else 0
        metrics["own_latency_ms"] = round(sum(owns) / len(owns), 1) if owns else None
        metrics["ecart_index"] = dict(_ecart_idx)
        ***REMOVED*** Le PIRE des sources : un proxy qui a dérivé ne doit pas être noyé par les sains.
        _pubs = [q for q in (sd.publish() for sd in list(_delai_etage.values())) if q]
        metrics["delai_etage_trames"] = (max(_pubs, key=lambda q: q.get("vieux_moy") or 0)
                                         if _pubs else None)
        if _vent[0]:
            _o0, _em, _cm = _vent[0]
            metrics["slice_breakdown_ms"] = {{"emit": round(_em / 1e6, 2),
                                            "commit": round(_cm / 1e6, 2),
                                            "mvk_ok": _mvk_stat["ok"], "mvk_ko": _mvk_stat["ko"]}}
