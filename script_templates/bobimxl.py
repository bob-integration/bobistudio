#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
bobimxl — binding Python (ctypes) du SDK MXL (Media eXchange Layer, Linux Foundation).

CHANTIER MXL — PHASE 0 (fondations + preuve free-run). Cette lib est le pont entre nos
plugins Python (numpy) et `libmxl` (API C). Elle est COPIÉE dans les images runtime Docker
au même titre qu'`agent.py` (cf. plugins/_compute_runtime/Dockerfile) → un seul exemplaire
partagé par tous les scripts plugins (fin de la duplication du code shm inline).

EXIGENCE DURE (cf. plan Phase 0) : l'adoption MXL ne doit JAMAIS forcer un producteur à
attendre une grille de synchro. Le chemin « sortie au plus tôt » reste de première classe :
  - Writer free-run  : `index_mode="free"` → l'index de grain est un compteur libre interne ;
                       on commit dès que la frame est prête, aucune attente d'horloge.
  - Reader get-latest: `Reader.get_latest()` est NON-BLOQUANT → dernier grain publié, sans
                       se caler sur une grille (latence cible ≤ poll mmap actuel, 1-2 ms).
Le futex bloquant de MXL (`Reader.get(index, timeout)`) REMPLACE le poll pour les
consommateurs qui *veulent* se caler ; il ne remplace pas le free-run. Le genlock reste
opt-in et par-chaîne (cf. broadcast-chain-genlock).

API C de référence (SDK dmf-mxl/mxl, en-têtes lib/include/mxl/) :
  mxl.h      : mxlCreateInstance/mxlDestroyInstance/mxlGarbageCollectFlows/mxlGetVersion
  flow.h     : mxlCreateFlowWriter/Reader, OpenGrain/CommitGrain/CancelGrain,
               GetGrain (bloquant) / GetGrainNonBlocking, GetRuntimeInfo
  time.h     : mxlGetTime, mxlGetCurrentIndex, mxlTimestampToIndex/IndexToTimestamp
  rational.h : mxlRational {int64 numerator; int64 denominator}

⚠ Cette lib ne peut PAS être validée hors d'un nœud disposant de libmxl + d'un tmpfs MXL.
La validation se fait au BANC (harnais script_templates/mxl_bench_*.py) — cf. plan Phase 0.
"""

import ctypes
from collections import deque
import json
import os
import re
import shutil
import sys
import time
import uuid

import numpy as np

# Version de contrat lib(image) ↔ scripts plugins : un script qui exige une version
# supérieure à celle embarquée dans l'image refuse de démarrer (anti-skew, cf. plan).
API_VERSION = 1

# True si la lib expose l'API audio (samples) — positionné par _bind (tolérant aux builds sans).
HAS_AUDIO = False

# True si la lib expose l'API de lecture PARTIELLE par tranches (mxlFlowReaderGetGrainSlice*,
# libmxl ≥ v1.1.0) — positionné par _bind. Chantier latence sous-trame (DPDK_NARROW Phase 3) :
# writer qui committe PROGRESSIVEMENT (validSlices=1..N) + readers réveillés à chaque bande.
HAS_SLICES = False

# minValidSlices « toutes les tranches » (flow.h: MXL_GRAIN_VALID_SLICES_ALL) — un get_slice
# avec cette valeur équivaut au get() plein-grain historique.
MXL_GRAIN_VALID_SLICES_ALL = 0xFFFF

# UUIDv5 déterministe : l'orchestrateur garde les NOMS comme identifiant UX/DB ; l'UUID
# MXL en est dérivé, jamais stocké ni montré (cf. identity-addressing-full-docker).
# Namespace dédié Bobi.Studio (uuid5 d'un FQDN projet sous le namespace DNS standard).
_NS_BOBI = uuid.uuid5(uuid.NAMESPACE_DNS, "mxl.bobi.studio")

# Domaine MXL = sous-répertoire du tmpfs bind-monté (cf. docker_driver.mxl_mount → /dev/shm).
# Surchargeable par l'env pour isoler un banc (ex. /dev/shm/mxl-bench) sans toucher la prod.
DEFAULT_DOMAIN = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")

# ── Lot de synchronisation RDMA (`maxSyncBatchSizeHint`) ─────────────────────────────────────
# ★ MESURÉ le 2026-08-09 (banc dell-1 → dl360-1, 1080p50 tranché en 30 bandes, horodatage écrit
# DANS chaque tranche, décalage d'horloge TAI/UTC de 37,001 s soustrait) :
#
#   lot 30 (= totalSlices, LE DÉFAUT DU SDK) : 1ʳᵉ bande lisible sur la réplique à 22,63 ms
#   lot  2                                   : 0,54 ms          lot 1 : 0,06 ms
#
# L'initiateur RDMA attend d'avoir `maxSyncBatchSizeHint` tranches avant de transférer. Au défaut
# il attend donc la TRAME ENTIÈRE : trancher un flux répliqué n'apporte alors rien sur le fil, la
# granularité sous-trame existe dans le format et n'est pas exploitée. À petit lot, le transfert
# devient concomitant à la production — une trame pleine de latence en moins.
#
# Coût mesuré à l'échelle (12 flux tranchés en parallèle, lot 2 contre lot 30) : débit et nombre de
# paquets IDENTIQUES (4,12 vs 4,13 Gb/s ; 487k vs 485k paquets/s) ; seul le CPU des initiateurs
# monte, de 15 % à 44 % CUMULÉS sur douze conteneurs — moins d'un demi-cœur. Et le retard de
# réplique s'AMÉLIORE (0 trame médian contre +1).
#
# Non posé = comportement historique (défaut SDK). Cesse de valoir si l'initiateur change sa
# politique de lot (`demo.cpp`, `slicesPerBatch`) ou si le nombre de flux répliqués change d'ordre.
MXL_SYNC_BATCH = os.environ.get("MXL_SYNC_BATCH", "").strip()


def _flow_options():
    """JSON d'options passé à `mxlCreateFlowWriter` (3ᵉ argument), ou None. Ne concerne QUE les
    flux créés ensuite — un flux existant garde le lot fixé à sa création (`mxl-info` → ligne
    `Sync batch size`)."""
    if not MXL_SYNC_BATCH:
        return None
    try:
        n = int(MXL_SYNC_BATCH)
    except (TypeError, ValueError):
        return None
    return json.dumps({"maxSyncBatchSizeHint": max(1, n)}).encode() if n > 0 else None

MXL_STATUS_OK = 0
MXL_UNDEFINED_INDEX = (1 << 64) - 1  # UINT64_MAX — « pas encore de grain » côté headIndex

# Décalage TAI↔UTC (secondes) centralisé et fail-fast (cf. risque temporel du plan).
# mxlGetTime() rend des ns TAI (epoch ST 2059) ; notre media_ts (mtl_rx.c) est déjà TAI →
# pas de conversion sur le chemin MXL. Le décalage n'est utile QU'aux frontières UTC.
TAI_UTC_OFFSET_S = int(os.environ.get("MXL_TAI_UTC_OFFSET", "37"))


# --------------------------------------------------------------------------- ctypes ABI

class mxlRational(ctypes.Structure):
    _fields_ = [("numerator", ctypes.c_int64), ("denominator", ctypes.c_int64)]


class mxlGrainInfo(ctypes.Structure):
    # flow.h : doit faire exactement 4096 octets (reserved dimensionné pour).
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("index", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("grainSize", ctypes.c_uint32),
        ("totalSlices", ctypes.c_uint16),
        ("validSlices", ctypes.c_uint16),
        ("reserved", ctypes.c_uint8 * 4068),
    ]


class mxlFlowRuntimeInfo(ctypes.Structure):
    # flowinfo.h : headIndex = index du dernier grain commité (clé du get-latest).
    _fields_ = [
        ("headIndex", ctypes.c_uint64),
        ("lastWriteTime", ctypes.c_uint64),
        ("lastReadTime", ctypes.c_uint64),
        ("reserved", ctypes.c_uint8 * 40),
    ]


class mxlVersionType(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint16), ("minor", ctypes.c_uint16),
        ("bugfix", ctypes.c_uint16), ("build", ctypes.c_uint16),
        ("full", ctypes.c_char_p),
    ]


# --- AUDIO : flux CONTINU de samples (flow.h) — buffers par-canal avec wrap d'anneau (2 fragments).
class mxlMutableBufferSlice(ctypes.Structure):
    _fields_ = [("pointer", ctypes.c_void_p), ("size", ctypes.c_size_t)]

class mxlBufferSlice(ctypes.Structure):
    _fields_ = [("pointer", ctypes.c_void_p), ("size", ctypes.c_size_t)]

class mxlMutableWrappedBufferSlice(ctypes.Structure):
    _fields_ = [("fragments", mxlMutableBufferSlice * 2)]   # [0]=contigu, [1]=partie wrap

class mxlWrappedBufferSlice(ctypes.Structure):
    _fields_ = [("fragments", mxlBufferSlice * 2)]

class mxlMutableWrappedMultiBufferSlice(ctypes.Structure):
    _fields_ = [("base", mxlMutableWrappedBufferSlice),
                ("stride", ctypes.c_size_t), ("count", ctypes.c_size_t)]   # count = canaux

class mxlWrappedMultiBufferSlice(ctypes.Structure):
    _fields_ = [("base", mxlWrappedBufferSlice),
                ("stride", ctypes.c_size_t), ("count", ctypes.c_size_t)]


assert ctypes.sizeof(mxlGrainInfo) == 4096, ctypes.sizeof(mxlGrainInfo)

_VOID = ctypes.c_void_p
_PU8 = ctypes.POINTER(ctypes.c_uint8)


class MXLError(RuntimeError):
    def __init__(self, fn, status):
        self.status = status
        super().__init__(f"{fn} -> mxlStatus {status}")


_lib = None


def _load(path=None):
    """Charge libmxl (paresseux). Chemin surchargeable par MXL_LIB_PATH."""
    global _lib
    if _lib is not None:
        return _lib
    candidates = [path or os.environ.get("MXL_LIB_PATH"),
                  "libmxl.so", "libmxl.so.1", "/usr/local/lib/libmxl.so",
                  "/usr/lib/libmxl.so"]
    last = None
    for c in candidates:
        if not c:
            continue
        try:
            _lib = ctypes.CDLL(c)
            break
        except OSError as e:
            last = e
    if _lib is None:
        raise OSError(f"libmxl introuvable (essayé {candidates}): {last}")
    _noter_variante()
    _bind(_lib)
    return _lib


# Chemin RÉELLEMENT chargé + variante déduite. Renseigné une fois, au chargement.
_lib_info = {"path": None, "variante": None}


def _noter_variante():
    """Retient QUEL fichier l'éditeur de liens a réellement ouvert pour libmxl.

    ★ POURQUOI. L'image embarque deux variantes — une baseline et une `x86-64-v3` (AVX2) dans
    `glibc-hwcaps/` — et c'est l'éditeur de liens qui tranche selon le CPU. Rien ne le disait :
    un basculement silencieux sur la baseline coûterait ~20 % sur un régime consommateur (mesuré)
    sans qu'aucun indicateur ne bouge. Le risque est concret : les deux DERNIERS candidats de la
    liste ci-dessus sont des chemins ABSOLUS, qui court-circuitent hwcaps — si la résolution par
    SONAME venait à échouer, le repli chargerait la baseline et tout continuerait de tourner.

    On lit donc /proc/self/maps APRÈS le chargement : c'est le chemin effectivement mappé, pas
    celui qu'on a demandé."""
    try:
        with open("/proc/self/maps") as f:
            chemins = sorted({l.split()[-1] for l in f if "libmxl.so" in l})
    except OSError:
        return
    if not chemins:
        return
    p = chemins[0]
    _lib_info["path"] = p
    _lib_info["variante"] = "x86-64-v3" if "glibc-hwcaps" in p else "baseline"


def lib_info():
    """{path, variante} de la libmxl chargée — à publier dans l'état des plugins, à côté de
    `plugin_version`. `variante` vaut "x86-64-v3", "baseline", ou None si indéterminable."""
    return dict(_lib_info)


def _bind(lib):
    """Déclare argtypes/restype — indispensable en 64 bits (sinon troncature de pointeurs)."""
    lib.mxlCreateInstance.restype = _VOID
    lib.mxlCreateInstance.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.mxlDestroyInstance.restype = ctypes.c_int
    lib.mxlDestroyInstance.argtypes = [_VOID]
    lib.mxlGarbageCollectFlows.restype = ctypes.c_int
    lib.mxlGarbageCollectFlows.argtypes = [_VOID]
    lib.mxlGetVersion.restype = ctypes.c_int
    lib.mxlGetVersion.argtypes = [ctypes.POINTER(mxlVersionType)]

    lib.mxlCreateFlowWriter.restype = ctypes.c_int
    lib.mxlCreateFlowWriter.argtypes = [
        _VOID, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_VOID),
        _VOID, ctypes.POINTER(ctypes.c_bool)]
    lib.mxlReleaseFlowWriter.restype = ctypes.c_int
    lib.mxlReleaseFlowWriter.argtypes = [_VOID, _VOID]
    lib.mxlFlowWriterOpenGrain.restype = ctypes.c_int
    lib.mxlFlowWriterOpenGrain.argtypes = [
        _VOID, ctypes.c_uint64, ctypes.POINTER(mxlGrainInfo), ctypes.POINTER(_PU8)]
    lib.mxlFlowWriterCommitGrain.restype = ctypes.c_int
    lib.mxlFlowWriterCommitGrain.argtypes = [_VOID, ctypes.POINTER(mxlGrainInfo)]
    lib.mxlFlowWriterCancelGrain.restype = ctypes.c_int
    lib.mxlFlowWriterCancelGrain.argtypes = [_VOID]

    lib.mxlCreateFlowReader.restype = ctypes.c_int
    lib.mxlCreateFlowReader.argtypes = [_VOID, ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.POINTER(_VOID)]
    lib.mxlReleaseFlowReader.restype = ctypes.c_int
    lib.mxlReleaseFlowReader.argtypes = [_VOID, _VOID]
    lib.mxlFlowReaderGetGrain.restype = ctypes.c_int
    lib.mxlFlowReaderGetGrain.argtypes = [
        _VOID, ctypes.c_uint64, ctypes.c_uint64, ctypes.POINTER(mxlGrainInfo),
        ctypes.POINTER(_PU8)]
    lib.mxlFlowReaderGetGrainNonBlocking.restype = ctypes.c_int
    lib.mxlFlowReaderGetGrainNonBlocking.argtypes = [
        _VOID, ctypes.c_uint64, ctypes.POINTER(mxlGrainInfo), ctypes.POINTER(_PU8)]
    lib.mxlFlowReaderGetRuntimeInfo.restype = ctypes.c_int
    lib.mxlFlowReaderGetRuntimeInfo.argtypes = [_VOID, ctypes.POINTER(mxlFlowRuntimeInfo)]

    # LECTURE PARTIELLE par tranches (libmxl ≥ v1.1.0, flow.h). TOLÉRANT : symbole absent
    # (vieille lib) → HAS_SLICES reste False, le binding plein-grain fonctionne inchangé.
    global HAS_SLICES
    try:
        lib.mxlFlowReaderGetGrainSlice.restype = ctypes.c_int
        lib.mxlFlowReaderGetGrainSlice.argtypes = [
            _VOID, ctypes.c_uint64, ctypes.c_uint16, ctypes.c_uint64,
            ctypes.POINTER(mxlGrainInfo), ctypes.POINTER(_PU8)]
        lib.mxlFlowReaderGetGrainSliceNonBlocking.restype = ctypes.c_int
        lib.mxlFlowReaderGetGrainSliceNonBlocking.argtypes = [
            _VOID, ctypes.c_uint64, ctypes.c_uint16,
            ctypes.POINTER(mxlGrainInfo), ctypes.POINTER(_PU8)]
        HAS_SLICES = True
    except AttributeError:
        HAS_SLICES = False
    lib.mxlGetFlowDef.restype = ctypes.c_int
    lib.mxlGetFlowDef.argtypes = [_VOID, ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.POINTER(ctypes.c_size_t)]

    # AUDIO (samples continus). TOLÉRANT : un symbole audio absent (build sans audio) ne doit
    # PAS casser le binding vidéo → on capture AttributeError (HAS_AUDIO reste False).
    global HAS_AUDIO
    try:
        lib.mxlFlowWriterOpenSamples.restype = ctypes.c_int
        lib.mxlFlowWriterOpenSamples.argtypes = [
            _VOID, ctypes.c_uint64, ctypes.c_size_t,
            ctypes.POINTER(mxlMutableWrappedMultiBufferSlice)]
        lib.mxlFlowWriterCommitSamples.restype = ctypes.c_int
        lib.mxlFlowWriterCommitSamples.argtypes = [_VOID]
        lib.mxlFlowReaderGetSamples.restype = ctypes.c_int
        lib.mxlFlowReaderGetSamples.argtypes = [
            _VOID, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint64,
            ctypes.POINTER(mxlWrappedMultiBufferSlice)]
        lib.mxlFlowReaderGetSamplesNonBlocking.restype = ctypes.c_int
        lib.mxlFlowReaderGetSamplesNonBlocking.argtypes = [
            _VOID, ctypes.c_uint64, ctypes.c_size_t, ctypes.POINTER(mxlWrappedMultiBufferSlice)]
        HAS_AUDIO = True
    except AttributeError:
        HAS_AUDIO = False

    lib.mxlGetTime.restype = ctypes.c_uint64
    lib.mxlGetTime.argtypes = []
    lib.mxlGetCurrentIndex.restype = ctypes.c_uint64
    lib.mxlGetCurrentIndex.argtypes = [ctypes.POINTER(mxlRational)]
    lib.mxlTimestampToIndex.restype = ctypes.c_uint64
    lib.mxlTimestampToIndex.argtypes = [ctypes.POINTER(mxlRational), ctypes.c_uint64]
    lib.mxlIndexToTimestamp.restype = ctypes.c_uint64
    lib.mxlIndexToTimestamp.argtypes = [ctypes.POINTER(mxlRational), ctypes.c_uint64]


def _ck(fn, status):
    if status != MXL_STATUS_OK:
        raise MXLError(fn, status)


def _np_view(payload_ptr, size):
    """Vue numpy ZÉRO-COPIE sur le buffer d'un grain (pointeur ctypes → uint8[size])."""
    addr = ctypes.cast(payload_ptr, ctypes.c_void_p).value
    if not addr:
        raise MXLError("payload", "null pointer")
    buf = (ctypes.c_uint8 * size).from_address(addr)
    return np.frombuffer(buf, dtype=np.uint8)


# --------------------------------------------------------------------------- identité / flow_def

def flow_id(name: str) -> str:
    """UUIDv5 déterministe d'un nom de flux (= le champ `id` du flowDef, et le flowId lecteur)."""
    return str(uuid.uuid5(_NS_BOBI, name))


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_flow_uuid(s) -> bool:
    """Vrai si `s` est un flowId UUID littéral (un flux TIERS a un UUID arbitraire, non
    reconstructible par uuid5 — chantier interop, lecture par-flowId brut)."""
    return bool(s) and bool(_UUID_RE.match(str(s).strip()))


def discover_flows(domain=None):
    """DÉCOUVERTE du domaine (interop) : énumère `<domain>/*.mxl-flow/flow_def.json` et renvoie
    la liste des flowDefs parsés `[{"id", "label", "media_type", "format", "def"}, …]` — y
    compris les flux publiés par des containers TIERS (dont l'UUID n'est pas un uuid5 de nom
    maison). Lecture filesystem pure (aucun appel libmxl) ; un flow_def illisible est ignoré."""
    domain = domain or DEFAULT_DOMAIN
    out = []
    try:
        entries = sorted(os.listdir(domain))
    except OSError:
        return out
    for e in entries:
        if not e.endswith(".mxl-flow"):
            continue
        try:
            with open(os.path.join(domain, e, "flow_def.json")) as f:
                fd = json.load(f)
        except Exception:
            continue
        out.append({
            "id": fd.get("id") or e[:-len(".mxl-flow")],
            "label": fd.get("label") or "",
            "media_type": fd.get("media_type") or "",
            "format": fd.get("format") or "",
            "def": fd,
        })
    return out


# Mapping chroma → (sous-échantillonnage horizontal Cb/Cr). 444=1, 422=2, 420=2 (vertical géré
# par la hauteur des composants). Payload PLANAR conservé octet-identique (cf. plan : pas de
# v210 — pack/unpack numpy rédhibitoire à 50 fps). En 10 bits on stocke PLANAR10LE (16 b/sample),
# comme aujourd'hui côté mtl_rx (v>>2 / planar10). La cohérence grainSize MXL ↔ taille planar
# réelle est un CRITÈRE DE BANC (Phase 0) — voir frame_bytes().
_CHROMA = {"444": (1, 1), "422": (2, 1), "420": (2, 2)}


def frame_bytes(width, height, chroma="422", bit_depth=8):
    """Taille d'une frame planar (octets) selon NOTRE contrat — sert à dimensionner/valider
    le grain. 8 bits = 1 o/sample ; 10/12 bits = 2 o/sample (PLANAR{N}LE)."""
    hx, hy = _CHROMA.get(str(chroma), (2, 1))
    bps = 1 if int(bit_depth) <= 8 else 2
    luma = width * height
    chroma_px = (width // hx) * (height // hy)
    return (luma + 2 * chroma_px) * bps


def build_flow_def(name, width, height, chroma="422", bit_depth=10,
                   fps_num=50, fps_den=1, interlace="progressive",
                   colorspace="BT709", label=None, media_type="video/x-mxl-planar",
                   slice_height=0):
    """Construit le flowDef JSON (ressource Flow NMOS IS-04) attendu par mxlCreateFlowWriter.

    `id` = flow_id(name) (déterministe). **DÉFAUT = `video/x-mxl-planar`** : type PLANAR ajouté
    à libmxl par notre patch (cf. plugins/_compute_runtime/patches/mxl-planar-type.patch) →
    grain = somme des plans Y+Cb+Cr (octet-identique à notre shm maison), 1 slice (trame pleine),
    calcul pixel direct sans dé-packer. MXL stock n'accepte que `video/v210`/`video/v210a`
    (passer media_type pour ces cas d'interop aux frontières). Décision et chiffrage : aide
    « MXL (bus shm) & format vidéo ». Champs REQUIS par le parser : id, format, grain_rate,
    label, tags(grouphint), frame_width/height, media_type, components.
    """
    hx, hy = _CHROMA.get(str(chroma), (2, 1))
    cw, ch = width // hx, height // hy
    # slice_height > 0 → le grain planar est déclaré en N = hauteur/slice_height TRANCHES égales
    # (patch mxl-planar-slices, latence sous-trame : commit progressif validSlices=1..N).
    # 0/absent → 1 slice (trame pleine), comportement historique — les libs sans le patch
    # ignorent simplement le champ. Le découpage est en OCTETS contigus du payload : à charge
    # de l'app d'y ranger un layout par-bande (ex. bande i = Y_i+Cb_i+Cr_i contigus).
    extra = {"slice_height": int(slice_height)} if slice_height else {}
    return json.dumps({
        **extra,
        "id": flow_id(name),
        # MXL FlowParser EXIGE le tag group-hint NMOS (« <groupe>:<rôle> ») — sinon
        # « Invalid or missing group hint tag » au mxlCreateFlowWriter.
        "tags": {"urn:x-nmos:tag:grouphint/v1.0": ["%s:Video" % (label or name)]},
        "format": "urn:x-nmos:format:video",
        "label": label or name,
        "media_type": media_type,
        "grain_rate": {"numerator": int(fps_num), "denominator": int(fps_den)},
        "frame_width": int(width),
        "frame_height": int(height),
        "interlace_mode": interlace,
        "colorspace": colorspace,
        "components": [
            {"name": "Y", "width": int(width), "height": int(height), "bit_depth": int(bit_depth)},
            {"name": "Cb", "width": int(cw), "height": int(ch), "bit_depth": int(bit_depth)},
            {"name": "Cr", "width": int(cw), "height": int(ch), "bit_depth": int(bit_depth)},
        ],
    })


# --------------------------------------------------------------------------- Instance / Writer / Reader

class Instance:
    """Poignée MXL (un domaine = un sous-rép. tmpfs). À partager par tous les writers/readers
    d'un même process."""

    def __init__(self, domain=DEFAULT_DOMAIN, options=None):
        _load()
        os.makedirs(domain, exist_ok=True)
        self.domain = domain
        self._h = _lib.mxlCreateInstance(domain.encode(),
                                         options.encode() if options else None)
        if not self._h:
            raise OSError(f"mxlCreateInstance({domain}) a échoué (tmpfs ? droits ?)")

    def garbage_collect(self):
        """Récupère les flows dont le writer est mort (anti-fuite tmpfs) — critère banc GC."""
        _ck("mxlGarbageCollectFlows", _lib.mxlGarbageCollectFlows(self._h))

    def flow_def(self, name=None, fid=None):
        """Lit le flow_def (JSON) d'un flux par son NOM (uuid5 maison) ou directement par
        `fid` (flowId UUID brut — flux TIERS, chantier interop) → dict, ou None si absent.
        Source de vérité du format CÔTÉ DONNÉE (écrit par le producteur) — cf. plan Phase 1."""
        fid = (fid or flow_id(name)).encode()
        # Buffer généreux en un seul appel : le flow_def fait < 1 Ko. (L'appel de sizing avec
        # buffer NULL renvoie INVALID_ARG sur libmxl v1.0 — on évite ce chemin.)
        size = ctypes.c_size_t(65536)
        buf = ctypes.create_string_buffer(size.value)
        st = _lib.mxlGetFlowDef(self._h, fid, buf, ctypes.byref(size))
        if st != MXL_STATUS_OK:
            return None
        try:
            return json.loads(buf.value.decode())
        except Exception:
            return None

    def close(self):
        if getattr(self, "_h", None):
            _lib.mxlDestroyInstance(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Writer:
    """Producteur de flux. FREE-RUN par défaut : on commit dès que la frame est prête.

    index_mode :
      - "tai" (DÉFAUT)  : index = mxlGetCurrentIndex(grain_rate) → la grille TAI ST 2059. C'est
        le SEUL mode conforme au modèle de temps MXL pour un producteur live, et donc le défaut.
        N'attend pas (OpenGrain réclame un slot, il ne bloque pas sur le temps) : l'exigence
        « sortie au plus tôt » est intacte, la cadence reste au producteur.
      - "free"          : compteur libre. ⚠ **NON CONFORME** au modèle de temps MXL (cf.
        `next_index`) — flux irréplicable entre nœuds et alignement inter-flux cassé. À ne
        réserver qu'à un usage NON temps réel (traitement de fichier hors ligne), jamais à un
        producteur live.
      - "genlock"       : index = index courant du FLUX DE RÉFÉRENCE maison `ref` (un Reader),
        dont l'index vient du PHC via le moteur 2110_io (cf. docs/reference/PTP_CLOCK.md §4). Remplace la
        dépendance à l'horloge système : le producteur sort la trame d'index K = index de
        grille GM. La CADENCE (l'attente du prochain tick K + le holdover) est portée par
        `ref.wait_next_index()` — le producteur fait `k = ref.wait_next_index(last_k, ...)`
        puis `open_grain(index=k)`. Si `index=` est omis, `_next_index` retombe sur l'index de
        tête de `ref` (et sur le compteur libre tant que la réf n'existe pas encore → « snap »
        automatique dès qu'elle apparaît).
    """

    def __init__(self, instance: Instance, name, width, height, chroma="422",
                 bit_depth=8, fps_num=50, fps_den=1, index_mode="tai", flow_def=None,
                 ref=None, **flow_kw):
        _load()
        self.inst = instance
        self.name = name
        # Réf de genlock (un Reader du flux de référence maison) — utilisé si index_mode="genlock".
        self.ref = ref
        # flow_def override = flux non-vidéo (ex. data/ANC via build_data_flow_def) ; sinon vidéo.
        self.flow_def = flow_def or build_flow_def(name, width, height, chroma, bit_depth,
                                                   fps_num, fps_den, **flow_kw)
        self.frame_size = frame_bytes(width, height, chroma, bit_depth) if width and height else 0
        self.rate = mxlRational(int(fps_num), int(fps_den))
        self.index_mode = index_mode
        self._counter = 0
        self._h = _VOID()
        # ── GARDE FLOW PÉRIMÉ (« flowDef menteur ») ───────────────────────────────────────────
        # flow.h / mxlCreateFlowWriter : `created`=false ⇔ un flow EXISTANT est RÉATTACHÉ et le
        # flowDef qu'on vient de construire est IGNORÉ (sémantique = attachement, pas remplacement).
        # Un producteur reconfiguré (scan/résolution/cadence/chroma…) qui redémarre réattache donc
        # SILENCIEUSEMENT sur l'ANCIEN flowDef : le SHM déclare l'ancien format à tout l'aval (NMOS,
        # slot TX 2110, SDP) et ÉCRIT le nouveau — mesuré en prod (mur multiview 1080i50 resté
        # flowDef 1080p50 progressif après le fix scan du script, cf. multiview-interlace bug).
        # On compare aux champs STRUCTURANTS du flow_def existant et on purge son répertoire AVANT
        # create si ça diverge → recréation propre (à charge des consommateurs, qui gèrent déjà la
        # reconnexion SIGBUS d'un flow recréé au redémarrage d'un producteur).
        _new_fd = json.loads(self.flow_def)
        _old_fd = instance.flow_def(name)
        if _old_fd is not None and any(_old_fd.get(k) != _new_fd.get(k) for k in
                ("media_type", "interlace_mode", "frame_width", "frame_height",
                 "grain_rate", "components", "slice_height")):
            _stale_dir = os.path.join(instance.domain, flow_id(name) + ".mxl-flow")
            try:
                shutil.rmtree(_stale_dir)
            except FileNotFoundError:
                pass
            except Exception as _e:
                print(f"bobimxl: purge du flow périmé {name!r} échouée ({_e!r}) — "
                      f"mxlCreateFlowWriter risque de rattacher l'ancien flowDef")
        created = ctypes.c_bool(False)
        _ck("mxlCreateFlowWriter", _lib.mxlCreateFlowWriter(
            instance._h, self.flow_def.encode(), _flow_options(),
            ctypes.byref(self._h), None, ctypes.byref(created)))
        self.created = bool(created.value)

    def next_index(self, src_index=None, lookahead=0):
        """Index du prochain grain de sortie — **LE point unique où il se calcule**, pour toute la
        flotte. Un plugin ne doit JAMAIS refaire ce calcul chez lui : ni compteur maison, ni appel
        direct à mxlGetCurrentIndex, ni recopie brute de l'index d'une entrée.

        `src_index` = index du grain SOURCE dont celui-ci est le TRAITEMENT. Le fournir quand on
        transforme un grain existant (correcteur, convertisseur, retard, proxy…) : le grain de
        sortie porte alors la coordonnée temporelle de son ORIGINE, ce que la doc appelle l'origin
        timestamp — c'est ce qui garde une chaîne à plusieurs branches alignée, quelles que soient
        les latences respectives. L'omettre quand on GÉNÈRE (mire, mur, mélangeur, playout) : la
        coordonnée est alors « maintenant ».

        Ne pas fournir `src_index` quand il n'y a pas de source (entrée absente, trame de garde,
        noir de remplissage) : le repli sur la grille est décidé ICI, une fois pour toutes, au lieu
        d'être réinventé par chaque plugin.

        `lookahead` = nombre de grains D'AVANCE sur la grille. Un producteur cadencé qui veut
        DORMIR jusqu'à l'instant de sa prochaine trame doit viser un créneau encore à venir, sinon
        l'échéance est déjà passée quand il se réveille. Il alloue donc `next_index(lookahead=1)`
        puis dort jusqu'à `index_time_ns()` de cet index.

        ⚠ **`lookahead=1`, JAMAIS PLUS, pour ce motif « allouer puis dormir jusqu'à l'échéance ».**
        L'avance est consommée PAR LE SOMMEIL : on se réveille AU créneau visé, donc l'appel
        suivant repart d'une grille qui a déjà avancé de L, et lui rajoute L. La tête avance de L
        créneaux par tour et **la cadence est divisée par L** — silencieusement, sans qu'aucun
        compteur interne ne s'en émeuve (le flux reste sur la grille, strictement croissant, et
        `own_latency_ms` reste bas puisque le rendu, lui, va vite).
        Mesuré le 2026-08-02 sur `avsync` et `stills`, qui portaient tous deux `lookahead=2` :
        **25,0 fps pile pour 50 demandés** sur trois microarchitectures, avec 8 ms de rendu pour
        20 ms de budget. Les plugins qui appellent `next_index()` sans avance (`mixer`,
        `multiview`) tenaient 50,0 sur la même flotte au même instant.
        Une avance plus grande n'a de sens que pour un producteur PIPELINÉ, qui prépare la trame
        k+L pendant que la grille est à k et ne dort PAS jusqu'à l'échéance de k+L — cas qui
        n'existe nulle part dans la flotte aujourd'hui.

        Le rattrapage est INCLUS : à chaque appel on repart de la grille COURANTE (le `max` ne
        garde que la stricte croissance), donc un creux d'ordonnancement se résorbe tout seul — pas
        besoin du compteur incrémenté à la main et de sa garde « si je suis en retard » que les
        plugins portaient chacun de leur côté.

        L'index d'un grain est une COORDONNÉE DE TEMPS, pas un numéro de séquence. `docs/Timing.md`
        du SDK est normatif : « Each index of the ring buffer correspond to a timestamp relative to
        the PTP epoch as defined by SMPTE 2059-1 », soit `GrainIndex = Timestamp / GrainDurationNs`.
        Un compteur parti de 0 produit donc des grains qui prétendent dater de l'epoch 2059 : ça
        casse l'alignement inter-flux (que la doc décrit comme `ReadIndex = min(F1_head … FN_head)`)
        et ça rend le flux irréplicable vers un autre nœud — la réplication s'amorce sur la grille
        et n'atteint jamais un index parti de 0, sans un log.

        La CADENCE est un tout autre sujet : elle dit QUAND on émet, jamais quelle coordonnée on
        estampille. Cette méthode N'ATTEND JAMAIS (elle lit une horloge) — l'exigence « sortie au
        plus tôt » d'un mélangeur ou d'un mur est intacte.

        **STRICTEMENT CROISSANT par construction** (`max` avec le cran précédent). Sans ça : deux
        trames émises dans le même créneau de grille (cadence plus rapide que la grille), une
        horloge qui recule sur un pas NTP, ou une horloge indisponible rendraient deux fois le même
        index — la tête du flux se figerait et le flux passerait pour MORT chez ses consommateurs.
        À l'inverse, une référence de genlock qui décroche ou piétine fait avancer d'un cran
        (holdover) et le recalage sur la grille est immédiat à son retour.

        Un producteur qui a besoin de l'index (index de champ = `next_index() * 2 + parité`, écriture
        sur plusieurs writers au même index) l'appelle UNE fois par trame et passe la valeur en
        `index=` ; sinon `open_grain()` sans argument l'appelle pour lui."""
        if src_index is not None:
            # PROPAGATION : la coordonnée vient du grain source. Le max() ci-dessous s'applique
            # quand même — une source qui repart en arrière (producteur redémarré) ne doit pas
            # faire reculer notre tête, sous peine de réécrire des grains déjà publiés.
            candidat = int(src_index)
        elif self.index_mode == "genlock" and self.ref is not None:
            # Index = tête du flux de référence (index de grille GM, issu du PHC). Tant que la réf
            # n'existe pas (démarrage), 0 → le max() ci-dessous free-run, et on « snappe » sur la
            # grille dès que la réf apparaît.
            h = self.ref.head_index()
            candidat = int(h) if h != MXL_UNDEFINED_INDEX else 0
        elif self.index_mode == "free":
            candidat = 0                       # compteur pur — NON CONFORME, cf. l'avertissement
        else:                                  # "tai" (défaut) : la grille
            g = int(_lib.mxlGetCurrentIndex(ctypes.byref(self.rate)))
            candidat = g if 0 < g < MXL_UNDEFINED_INDEX else 0
        if candidat and lookahead:
            candidat += int(lookahead)
        self._counter = max(candidat, self._counter + 1)
        return self._counter

    # Alias historique : des scripts appellent encore `_next_index()`. Même méthode, mêmes garanties.
    _next_index = next_index

    def index_time_ns(self, index):
        """Instant TAI (ns depuis l'epoch ST 2059) du grain `index`, à la cadence de CE flux.

        Pendant de `next_index` : l'un donne la coordonnée, l'autre l'instant où elle échoit. Un
        producteur cadencé alloue `next_index(lookahead=N)` puis dort jusqu'à `index_time_ns()` de
        cet index — plus de conversion index↔temps réécrite dans chaque plugin, et surtout plus de
        grille calculée sur CLOCK_REALTIME (qui vaut l'UTC sur un nœud correctement synchronisé,
        soit 37 s à côté de la grille TAI de tous les autres producteurs)."""
        return int(_lib.mxlIndexToTimestamp(ctypes.byref(self.rate), int(index)))

    def open_grain(self, index=None, src_index=None):
        """Réclame un grain et renvoie (index, grainInfo, vue_numpy_écrivable ZÉRO-COPIE).
        Le producteur peut rendre DIRECTEMENT dans la vue (vrai zéro-copie) puis commit().

        Sans argument : la coordonnée est calculée par `next_index()` (grille TAI). `src_index=` :
        propagation depuis le grain source traité (cf. `next_index`). `index=` : coordonnée imposée
        — réservé aux cas qui la DÉRIVENT d'un appel à `next_index()` (index de champ = base×2 +
        parité, plusieurs writers au même index), jamais pour y remettre un compteur maison."""
        idx = self.next_index(src_index) if index is None else index
        gi = mxlGrainInfo()
        payload = _PU8()
        _ck("mxlFlowWriterOpenGrain", _lib.mxlFlowWriterOpenGrain(
            self._h, idx, ctypes.byref(gi), ctypes.byref(payload)))
        view = _np_view(payload, gi.grainSize or self.frame_size)
        return idx, gi, view

    def commit(self, grain_info, valid_slices=None):
        """Publie le grain. **CRUCIAL** : marquer le grain COMPLET (`validSlices == totalSlices`)
        — sinon le lecteur le voit partiel et `getGrain` renvoie OUT_OF_RANGE_TOO_EARLY (grain
        jamais lisible). Plein-frame par défaut ; `valid_slices` pour un write par tranches."""
        grain_info.validSlices = (grain_info.totalSlices if valid_slices is None
                                  else int(valid_slices))
        _ck("mxlFlowWriterCommitGrain",
            _lib.mxlFlowWriterCommitGrain(self._h, ctypes.byref(grain_info)))

    def write(self, frame: np.ndarray, index=None):
        """Convenance : copie `frame` (1-D uint8 ou n'importe quelle vue contiguë) dans le grain
        et commit. Pour le vrai zéro-copie, préférer open_grain()+rendu en place+commit()."""
        idx, gi, view = self.open_grain(index)
        blit(view, frame)
        self.commit(gi)
        return idx

    def close(self):
        if getattr(self, "_h", None):
            _lib.mxlReleaseFlowWriter(self.inst._h, self._h)
            self._h = _VOID()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


_AUTO_REOPENS = 0


def auto_reopen_count():
    """Nombre de réouvertures AUTOMATIQUES depuis le démarrage du script. À publier dans les
    métriques du plugin : une valeur qui grimpe dit qu'un producteur est recréé en boucle, ce
    qu'aucun compteur d'images ne montrerait (les images, elles, continuent d'arriver)."""
    return _AUTO_REOPENS


class _StaleGuard:
    """DÉCROCHAGE DE GÉNÉRATION — parade PARTAGÉE par tous les consommateurs longue durée.

    Un producteur qui DÉTRUIT puis RECRÉE son flux SOUS LE MÊME NOM (changement de source d'un
    slot RX — mtl_rx fusionne/dégroupe alors ses sessions —, redéploiement, reconfiguration) laisse
    tous les Readers ouverts accrochés à la génération MORTE. Le piège : cette génération reste
    LISIBLE (grains servis, index figé) → **aucun SIGBUS, aucune exception**, donc les reprises
    « sur SIGBUS » ou « sur exception » ne se déclenchent JAMAIS. Le consommateur gèle en silence,
    pour toujours (mesuré en prod : 3 h 20 sur un shard de mur, image parfaite à l'écran).

    Détecteur conforme à la spec MXL : `now_tai() − lastWriteTime` croît alors que l'horloge
    avance. (Les writers AUDIO ne bumpent pas lastWriteTime : côté audio, le signe est un
    `head_index` qui ne bouge plus — cf. AudioReader.reopen_if_head_stale.)

    Récupération, dans cet ordre — vérifié au banc :
      1. lâcher NOTRE handle (sans ça le flux reste RÉFÉRENCÉ et le GC ne le réclame pas) ;
      2. `garbage_collect()` (sinon le nom résout encore vers l'orphelin) ;
      3. rouvrir. Si une référence subsiste malgré tout dans notre Instance, la réouverture
         retombe sur l'orphelin → au 2ᵉ essai on rouvre sur une Instance **DÉDIÉE** (cache
         vierge, résolution sur disque). Un lecteur d'un AUTRE process, lui, ne bloque rien.
    """

    STALE_MS_DEFAULT = 5000.0     # ~250 trames à 50 Hz : au-delà, aucune source vivante n'est plausible
    AUTO_PERIODE_S = 1.0          # cadence du contrôle implicite (le coût est un runtime_info/s)

    # ── Pourquoi la garde est IMPLICITE et non plus facultative ─────────────────────────────
    # Cette parade a longtemps été en opt-in : chaque consommateur devait penser à appeler
    # `reopen_if_stale`. Audit du 2026-08-22 : SEPT consommateurs sur onze ne l'appelaient pas
    # (delay, mixer, recorder, sonde_latence, split, udc, v210_bridge) — ils gelaient donc en
    # silence, pour toujours, au premier redéploiement de leur producteur. Ce n'est pas une série
    # d'oublis : une protection facultative contre une panne SILENCIEUSE n'a aucun retour qui
    # rappelle qu'on l'a oubliée, donc elle SERA oubliée. Constaté en direct le même jour : la
    # sonde de latence lisait un anneau mort depuis 1 h 44, image parfaite par ailleurs, et on a
    # cherché le défaut dans le décodeur.
    # La garde tourne donc toute seule dans les chemins de lecture. `auto_reopen=False` reste
    # possible pour un consommateur qui veut piloter la réouverture lui-même.

    def _auto_garde(self, verif):
        """Exécute `verif` au plus une fois par AUTO_PERIODE_S. Ne lève jamais : une garde qui
        casserait la lecture qu'elle protège serait pire que la panne.

        ⚠ Ne s'arme QUE si ce lecteur a déjà servi quelque chose depuis son ouverture. C'est le
        discriminant entre les deux situations que « périmé » confond :
          • lecteur DÉCROCHÉ (ce qu'on corrige) — des grains SONT servis, index figé ;
          • producteur ARRÊTÉ — rien n'est servi du tout, et rouvrir n'y changera rien.
        Sans ce garde-fou, une source simplement éteinte (prévisualisation de mixer coupée)
        déclenchait une réouverture par seconde pour toujours. Le recul exponentiel ne suffisait
        pas : le consommateur recrée son Reader sur erreur, et le compteur repartait à zéro."""
        if not getattr(self, "auto_reopen", True):
            return False
        if not getattr(self, "_auto_vu", False):
            return False
        maintenant = time.monotonic()
        if maintenant < getattr(self, "_auto_du_ns", 0.0):
            return False
        rouvert = False
        try:
            rouvert = bool(verif())
        except Exception:
            pass
        # ── RECUL EXPONENTIEL sur un producteur qui ne revient pas ──────────────────────────
        # Un flux ARRÊTÉ (et pas recréé) reste indéfiniment « périmé » : sans recul, la garde le
        # rouvrait une fois par seconde pour toujours. Mesuré en test : 55 réouvertures en 55 s
        # sur une prévisualisation de mixer simplement éteinte, et autant de lignes de journal —
        # une protection qui hurle en continu finit par être filtrée, donc ignorée.
        # `_stale_n` est remis à 0 par la garde dès que le flux réécrit : une source qui revient
        # est donc reprise à la seconde près, seul l'acharnement est amorti.
        n = getattr(self, "_stale_n", 0)
        recul = min(2 ** max(0, n - 1), 60) if n else 1
        self._auto_du_ns = time.monotonic() + self.AUTO_PERIODE_S * recul
        return rouvert

    def _auto_trace(self, age, n):
        """Une réouverture AUTOMATIQUE doit laisser une trace, sinon on ne distingue pas « jamais
        déclenché » de « déclenche en boucle sans effet » — et on aurait déplacé le silence d'un
        cran au lieu de le supprimer."""
        global _AUTO_REOPENS
        _AUTO_REOPENS += 1
        # Journal ÉCONOME : les trois premières tentatives, puis une sur dix. Le compteur reste
        # exact dans `auto_reopen_count()` — c'est la TRACE qu'on rationne, pas la mesure.
        if n > 3 and n % 10:
            return
        try:
            sys.stderr.write("[bobimxl] flux « %s » : génération morte (%.0f ms), réouverture "
                             "automatique n°%d%s\n"
                             % (getattr(self, "name", "?"), age or 0.0, n,
                                " — producteur absent, espacement des tentatives" if n > 3 else ""))
            sys.stderr.flush()
        except Exception:
            pass

    def poll_stale(self):
        """Exécute la garde de génération SANS LIRE. Renvoie True si le flux a été rouvert.

        La garde implicite est armée dans les chemins de LECTURE (`get_latest`, `get`, `read_from`,
        `read_latest`) — ce qui suffit tant que le consommateur lit en continu. Un consommateur
        SÉQUENTIEL, lui, s'arrête de lire dès qu'il a rattrapé la tête… et c'est exactement l'état
        où un anneau mort le fige POUR TOUJOURS : head figé ⇒ « rien de neuf à lire » ⇒ plus aucun
        appel de lecture ⇒ la garde ne tourne jamais. La protection était donc armée sur le seul
        chemin que la panne rend inatteignable (mesuré sur `sonde_latence` : âge audio republié au
        centième près pendant des minutes, sur un anneau mort).

        À appeler dans la boucle du consommateur quand un passage ne lit rien. Même cadence et même
        recul exponentiel que la garde implicite — ce n'est pas un second mécanisme, c'est le même,
        atteignable depuis un chemin qui ne lit pas."""
        return bool(self._auto_garde(self._verif_generation))

    def stale_ms(self):
        """Âge (ms) de la dernière écriture producteur, ou None si l'info n'est pas publiée."""
        lw = self.last_write_time()
        return (now_tai() - lw) / 1e6 if lw else None

    def _open_handle(self):
        _ck("mxlCreateFlowReader", _lib.mxlCreateFlowReader(
            self.inst._h, self.fid.encode(),
            self._opts.encode() if getattr(self, "_opts", None) else None,
            ctypes.byref(self._h)))

    def _close_own_instance(self):
        own = getattr(self, "_own_inst", None)
        if own is not None:
            self._own_inst = None
            try: own.close()
            except Exception: pass

    def reopen(self, dedicated=None):
        """Reconnexion sur la génération VIVANTE. `dedicated=None` (défaut) escalade toute seule
        sur une Instance dédiée à partir du 2ᵉ essai consécutif. Renvoie le n° de tentative."""
        n = getattr(self, "_stale_n", 0) + 1
        self._stale_n = n
        if dedicated is None:
            dedicated = n >= 2
        try:
            if self._h:
                _lib.mxlReleaseFlowReader(self.inst._h, self._h)
        except Exception:
            pass
        self._h = _VOID()
        try:
            self.inst.garbage_collect()
        except Exception:
            pass
        if dedicated:
            old_own = getattr(self, "_own_inst", None)
            self.inst = self._own_inst = Instance(self.inst.domain)
            if old_own is not None:
                try: old_own.close()
                except Exception: pass
        self._open_handle()
        return n

    def _verif_generation(self):
        """Contrôle de génération PAR DÉFAUT (vidéo) : `lastWriteTime` qui ne bouge plus.
        `AudioReader` le redéfinit — les writers audio ne bumpent pas `lastWriteTime`, et là le
        signe est un `head_index` figé."""
        return self.reopen_if_stale(on_reopen=self._auto_trace)

    def reopen_if_stale(self, max_age_ms=None, on_reopen=None):
        """À appeler dans la boucle du consommateur. Reconnecte si la dernière écriture producteur
        remonte à plus de `max_age_ms` (défaut STALE_MS_DEFAULT). Renvoie True si reconnecté.
        `on_reopen(age_ms, n)` permet de JOURNALISER — à faire : sans trace, on ne distingue pas
        « jamais déclenché » de « boucle sans effet »."""
        thr = self.STALE_MS_DEFAULT if max_age_ms is None else float(max_age_ms)
        age = self.stale_ms()
        if age is None or age <= thr:
            if age is not None:
                self._stale_n = 0        # écriture fraîche → lecteur sain, escalade désarmée
            return False
        n = self.reopen()
        if on_reopen is not None:
            try: on_reopen(age, n)
            except Exception: pass
        return True


class StageDelay:
    """DÉLAI D'ÉTAGE en TRAMES — ce que ce module ajoute à la chaîne. Mesure DIRECTE.

    À ne pas confondre avec le temps de CALCUL (`own_latency_ms` = ts_out − ts_cycle_start), qui
    dit si le module a de la MARGE. Un étage peut calculer en 4 ms et retarder le signal de 2
    trames : ce qui domine le délai est la QUANTIFICATION par la cadence (lire un contenu daté N,
    publier en N+2), invisible dans une durée de calcul. Ce sont deux grandeurs, pas deux unités
    de la même. Cf. docs/reference/LATENCE_CHAINE.md.

    ⚠ La mesure passe par le TEMPS, jamais par une soustraction d'index brute. Un index n'est une
    coordonnée temporelle que sur la grille TAI ; un writer en `index_mode="free"` porte un
    compteur libre, et plusieurs plugins ouvrent leurs grains en « index tai (genlock) OU compteur
    libre » selon le câblage. `idx_out − idx_in` y donnerait un nombre qui a l'air d'un délai —
    c'est le raisonnement qui a produit quatre conclusions fausses le 2026-08-11.

    Quand une entrée n'est pas convertible (flux absent, cadence illisible), on ne publie RIEN
    plutôt qu'un zéro : « aucune mesure » n'est pas « aucun délai ». `non_mesurable` compte ces
    cas pour qu'une chaîne muette soit diagnosticable au lieu d'être silencieuse.

    Usage, au moment d'écrire le grain de sortie :

        _sd = StageDelay()
        ...
        idx_out, gi, vue = writer.open_grain()
        _sd.observe(writer, idx_out, [(reader, idx_in)])
        ...
        metrics["delai_etage_trames"] = _sd.publish()
    """

    # Bornes de PLAUSIBILITÉ. Passer par le temps ne suffit pas : `index_time_ns` convertit un
    # index LIBRE tout aussi volontiers qu'un index TAI, en rendant un instant absurde. Hors de
    # ces bornes la mesure ne décrit pas un étage mais un désaccord de base d'index — on ne publie
    # rien plutôt qu'un nombre qui a l'air d'un délai.
    #
    # ⚠ La borne haute est en TEMPS, pas en trames. Elle valait 120 trames (2,4 s à 50 Hz), seuil
    # pensé pour des étages intra-nœud — il aurait REJETÉ une mesure inter-site légitime (liaison
    # satellite ou WAN longue). 10 s laisse passer le transport réel tout en attrapant les
    # désaccords de base, qui se comptent en heures. Relevable par `ecart_max_ns` si besoin.
    ECART_MAX_NS = 10_000_000_000

    def __init__(self, n=30, peremption_ns=2_000_000_000, essence="video", ecart_max_ns=None):
        # Les écarts sont stockés en NANOSECONDES, pas en pas d'index : c'est la seule unité
        # commune à la vidéo (trames) et à l'audio (échantillons), donc la seule qui permette de
        # comparer les deux chaînes — l'écart A/V est exactement cette différence.
        self._recent = deque(maxlen=n)     # entrée la plus RÉCENTE → plancher de l'étage
        self._vieux = deque(maxlen=n)      # entrée la plus VIEILLE → délai réellement subi
        self._periode_ns = None            # dernier pas d'index observé (1 trame, ou 1 échantillon)
        self._last_ns = 0
        self._peremption_ns = peremption_ns
        self._essence = essence            # "video" → aussi publié en trames · "audio" → ms seules
        self._ecart_max_ns = ecart_max_ns if ecart_max_ns is not None else self.ECART_MAX_NS
        self.non_mesurable = 0
        self._propage = False

    def observe(self, writer, idx_out, entrees, propage=False):
        """`entrees` = [(reader, idx_in), …]. Renvoie True si la trame a pu être mesurée.

        `propage=True` quand le grain de sortie est ouvert avec `src_index=` : il PORTE alors la
        coordonnée de la source, donc l'écart d'index vaut 0 PAR CONSTRUCTION. Ce zéro est juste
        — un étage qui propage ne décale pas la coordonnée temporelle du contenu, et la mesure
        aux bandeaux du 2026-08-12 l'a confirmé (pyramide à 0,00) — mais il ne se lit PAS comme
        le zéro d'un étage qui re-cadence et se trouverait n'ajouter aucune trame. Le drapeau est
        remonté tel quel jusqu'à l'interface pour que les deux ne soient pas confondus.

        Multi-entrées : on retient les DEUX bornes. La plus récente donne le plancher de l'étage,
        la plus vieille le délai que la trame produite a réellement subi — c'est celle-ci qui
        décrit le signal tel qu'il sort, et que l'orchestrateur affiche.
        """
        try:
            # Writer en compteur LIBRE : son index n'est pas une coordonnée temporelle. Aucune
            # conversion n'a de sens ici, et la refuser tôt évite de publier une valeur inventée.
            if getattr(writer, "index_mode", None) == "free":
                self.non_mesurable += 1
                return False
            t_out = writer.index_time_ns(idx_out)
            if t_out is None:
                self.non_mesurable += 1
                return False
            # Période lue sur le writer lui-même : aucune cadence supposée ni codée en dur.
            periode = writer.index_time_ns(int(idx_out) + 1) - t_out
            if not periode:
                self.non_mesurable += 1
                return False
            ts = []
            for rd, idx_in in (entrees or ()):
                t_in = rd.index_time_ns(idx_in) if rd is not None else None
                if t_in is None:
                    self.non_mesurable += 1
                    return False           # une entrée non convertible invalide la TRAME entière
                ts.append(t_in)
            if not ts:
                # AUCUNE entrée liée pour cette trame (toutes refusées en amont, source coupée,
                # référence perdue). C'est un refus comme un autre et il doit se COMPTER : sans
                # ça, `publish()` rendait None et le silence redevenait indiscernable d'un module
                # qui ne mesure pas. Observé sur `mixer-test` (2026-08-19), qui émettait 50 fps
                # « sans entrée vivante » — la page n'en disait rien.
                self.non_mesurable += 1
                return False
            recent_ns = t_out - max(ts)
            vieux_ns = t_out - min(ts)
            # Borne BASSE dynamique : une demi-période. Elle tolère l'arrondi d'un étage à délai
            # nul sans laisser passer une sortie réellement ANTÉRIEURE à son entrée — et elle
            # s'adapte d'elle-même à l'audio, où la période est un échantillon (~21 µs) et non
            # une trame (20 ms).
            _min = -0.5 * periode
            if not (_min <= recent_ns <= self._ecart_max_ns
                    and _min <= vieux_ns <= self._ecart_max_ns):
                self.non_mesurable += 1
                return False
            self._recent.append(recent_ns)
            self._vieux.append(vieux_ns)
            self._periode_ns = periode
            self._propage = bool(propage)
            self._last_ns = time.time_ns()
            return True
        except Exception:
            # Un défaut de mesure ne doit JAMAIS tuer la boucle de production du plugin.
            self.non_mesurable += 1
            return False

    def _stat(self, d, f):
        if not d or (time.time_ns() - self._last_ns) > self._peremption_ns:
            return None
        return round(f(d), 2)

    def publish(self):
        """Dict prêt pour `:8080`, ou None si l'on n'a rien à dire du tout.

        Trois états, volontairement distincts :
        - mesure fraîche → dict complet (`vieux_moy` présent) ;
        - **aucune mesure MAIS des refus** → `{"non_mesurable": N}`, SANS `vieux_moy` : en amont
          ça reste « non mesuré » (le consommateur teste `vieux_moy`), mais sur `:8080` on voit
          enfin POURQUOI c'est muet. Sans ce cas, le compteur de refus était incrémenté à six
          endroits et n'atteignait jamais personne — le silence était indiscernable d'un module
          qui ne mesure pas. Constaté en déployant `mixer-test` (2026-08-19), dont l'entrée
          affichait un transit de −37 s : la garde de plausibilité refusait, à raison, mais
          muettement.
        - rien du tout → None.
        """
        moy_ns = self._stat(self._vieux, lambda d: sum(d) / len(d))
        if moy_ns is None:
            return {"non_mesurable": self.non_mesurable} if self.non_mesurable else None
        # Les écarts sont STOCKÉS en ns (seule unité commune vidéo/audio) et RENDUS dans l'unité
        # naturelle de l'essence : trames pour la vidéo — c'est ce que lit l'orchestrateur —,
        # millisecondes pour l'audio, où « une trame » n'a pas de sens (granularité 1 ms).
        # Les ms sont publiées dans les DEUX cas : c'est par elles que se compare l'écart A/V.
        per = self._periode_ns or 0
        _ms = lambda v: (None if v is None else round(v / 1e6, 3))
        out = {"ms_moy": _ms(moy_ns),
               "ms_max": _ms(self._stat(self._vieux, max)),
               "recent_ms_moy": _ms(self._stat(self._recent, lambda d: sum(d) / len(d))),
               "recent_ms_max": _ms(self._stat(self._recent, max)),
               "essence": self._essence,
               "propage": self._propage,
               "non_mesurable": self.non_mesurable}
        if self._essence == "video" and per:
            _tr = lambda v: (None if v is None else round(v / per, 2))
            out.update({"recent_moy": _tr(self._stat(self._recent, lambda d: sum(d) / len(d))),
                        "recent_max": _tr(self._stat(self._recent, max)),
                        "vieux_moy": _tr(moy_ns),
                        "vieux_max": _tr(self._stat(self._vieux, max))})
        return out


class Reader(_StaleGuard):
    """Consommateur de flux, identifié par le NOM du flux producteur (→ flow_id interne),
    ou par un flowId UUID BRUT (`by_id=True` — flux TIERS dont l'UUID n'est pas dérivable
    d'un nom maison, chantier interop). Commodité : si `name` est déjà un UUID littéral,
    il est pris comme flowId direct."""

    def __init__(self, instance: Instance, name, options=None, by_id=False):
        _load()
        self.inst = instance
        self.name = name
        self.fid = str(name).strip() if (by_id or is_flow_uuid(name)) else flow_id(name)
        self._opts = options       # conservé : la réouverture doit rendre le MÊME reader
        self._own_inst = None      # Instance dédiée si escalade (cf. _StaleGuard)
        self._stale_n = 0
        self._h = _VOID()
        _ck("mxlCreateFlowReader", _lib.mxlCreateFlowReader(
            instance._h, self.fid.encode(), options.encode() if options else None,
            ctypes.byref(self._h)))
        # État genlock (cf. wait_next_index). `holdover` = True quand la réf ne tique plus et
        # qu'on free-run en secours. Attributs présents dès la construction (introspection sûre).
        self.holdover = False
        self._gl_hold_idx = 0        # prochain index à émettre en free-run de secours
        self._gl_hold_due_ns = 0     # échéance monotone (ns) du prochain grain de secours
        self._rate = None            # cadence du flux, résolue à la 1re conversion index→temps

    def index_time_ns(self, index):
        """Instant TAI (ns depuis l'epoch ST 2059) du grain `index`, à la cadence de CE flux.

        Pendant de `Writer.index_time_ns`, côté LECTURE. Sa raison d'être est la mesure du DÉLAI
        D'UN ÉTAGE : `index_sortie − index_entrée` n'est licite que si les deux flux partagent
        cadence ET base d'index. Ce n'est pas garanti — un writer en `index_mode="free"` porte un
        compteur libre, et plusieurs plugins ouvrent leurs grains en « index tai (genlock) OU
        compteur libre » selon leur câblage. Soustraire ces index-là donne un nombre qui a l'air
        d'un délai : c'est le raisonnement par index qui a produit quatre conclusions successives,
        toutes fausses, le 2026-08-11.

        En passant par le TEMPS, la mesure reste juste même quand les deux flux n'ont ni la même
        cadence ni la même base :

            delai_ns = writer.index_time_ns(idx_out) - reader.index_time_ns(idx_in)

        Renvoie **None** si la cadence du flux n'est pas lisible (flux absent du domaine) — jamais
        0, qui se lirait comme un instant valide et rendrait un délai faux au lieu d'une absence
        de mesure. Cf. docs/reference/LATENCE_CHAINE.md.
        """
        if self._rate is None:
            f = self.format()
            if not f:
                return None
            try:
                self._rate = mxlRational(int(f["fps_num"]), int(f["fps_den"]))
            except (KeyError, TypeError, ValueError):
                return None
        return int(_lib.mxlIndexToTimestamp(ctypes.byref(self._rate), int(index)))

    def head_index(self):
        """Index du dernier grain commité (MXL_UNDEFINED_INDEX si rien encore)."""
        rt = mxlFlowRuntimeInfo()
        _ck("mxlFlowReaderGetRuntimeInfo",
            _lib.mxlFlowReaderGetRuntimeInfo(self._h, ctypes.byref(rt)))
        return rt.headIndex

    def last_write_time(self):
        """Instant TAI (ns) de la dernière écriture du producteur (runtime info) — pour la
        latence transit : (now_tai() - last_write_time())/1e6 ms. 0 si indispo."""
        rt = mxlFlowRuntimeInfo()
        st = _lib.mxlFlowReaderGetRuntimeInfo(self._h, ctypes.byref(rt))
        return rt.lastWriteTime if st == MXL_STATUS_OK else 0

    def format(self):
        """Format du flux LU DANS SON flow_def (source de vérité côté donnée) :
        {width,height,chroma,bit_depth,fps_num,fps_den} ou None si le flux n'existe pas.
        chroma déduit du sous-échantillonnage des composants (½,1)=422 (½,½)=420 (1,1)=444."""
        fd = self.inst.flow_def(fid=self.fid)   # par-flowId : marche aussi pour un flux tiers
        if not fd:
            return None
        try:
            comps = {c["name"]: c for c in fd.get("components", [])}
            y = comps.get("Y") or next(iter(fd["components"]))
            cb = comps.get("Cb") or comps.get("U") or y
            w, h = int(fd["frame_width"]), int(fd["frame_height"])
            hx = 2 if int(cb["width"]) * 2 <= w else 1
            hy = 2 if int(cb["height"]) * 2 <= h else 1
            chroma = {(1, 1): "444", (2, 1): "422", (2, 2): "420"}.get((hx, hy), "422")
            gr = fd.get("grain_rate") or {}
            gn, gd = int(gr.get("numerator", 25)), int(gr.get("denominator", 1))
            # ENTRELACÉ NATIF (modèle SDK MXL) : un grain = 1 CHAMP (½ hauteur), cadence = cadence
            # CHAMP (libmxl double la grain_rate du flowDef). On expose les dims/cadence de GRAIN
            # (`width`/`height`/`fps_*`) pour que les consommateurs reshape/pace SANS changement
            # (reshape(height,width) tombe sur le champ, pacing à la cadence champ). Les dims/cadence
            # de TRAME et l'interlace sont à part (`frame_*`, `interlace_mode`/`field_order`) — à
            # passer sur la SORTIE pour rester champ-natif de bout en bout. Progressif : inchangé.
            im = str(fd.get("interlace_mode") or "progressive")
            interlaced = im.startswith("interlaced")
            grain_h = h // 2 if interlaced else h
            frame_gn = gn                     # cadence TRAME (= grain_rate du flowDef)
            if interlaced:
                gn *= 2                        # cadence de GRAIN = cadence champ (libmxl double)
            return {
                # dims/cadence de GRAIN (= champ si entrelacé) → reshape(height,width)+pacing direct
                "width": w, "height": grain_h, "chroma": chroma,
                "bit_depth": int(y.get("bit_depth", 8)),
                "fps_num": gn, "fps_den": gd,
                # interlace + dims/cadence de TRAME → à passer sur le Writer de SORTIE (champ-natif)
                "interlace_mode": im, "interlaced": interlaced,
                "field_order": ("tff" if im == "interlaced_tff"
                                else "bff" if im == "interlaced_bff" else ""),
                "frame_width": w, "frame_height": h,
                "frame_fps_num": frame_gn, "frame_fps_den": gd,
            }
        except Exception:
            return None

    def get_latest(self):
        """NON-BLOQUANT — dernier grain disponible (chemin free-run « au plus tôt »).
        `headIndex` peut désigner le grain EN COURS d'écriture (pas encore commité) → on tente
        head puis head-1. Renvoie (index, grainInfo, vue_numpy) ou None si rien de lisible.

        Contrôle de génération IMPLICITE (1/s) : cf. _StaleGuard. Un flux recréé sous le même nom
        laisse ce Reader accroché à la génération morte, qui reste LISIBLE — sans cette garde, la
        boucle appelante tourne indéfiniment sur un anneau figé sans la moindre erreur."""
        self._auto_garde(self._verif_generation)
        idx = self.head_index()
        if idx == MXL_UNDEFINED_INDEX:
            return None
        for cand in (idx, idx - 1):
            if cand < 0:
                break
            gi = mxlGrainInfo()
            payload = _PU8()
            st = _lib.mxlFlowReaderGetGrainNonBlocking(
                self._h, cand, ctypes.byref(gi), ctypes.byref(payload))
            if st == MXL_STATUS_OK:
                self._auto_vu = True
                return cand, gi, _np_view(payload, gi.grainSize)
        return None

    def get(self, index, timeout_ns=100_000_000):
        """BLOQUANT (futex) — attend le grain `index` jusqu'à timeout_ns. Chemin « se caler »
        (genlock), remplace le poll. Renvoie (index, grainInfo, vue_numpy) ou None si timeout.

        Même garde de génération que `get_latest` : un consommateur en genlock n'appelle que
        wait_next_index + get, et n'aurait sinon aucun contrôle."""
        self._auto_garde(self._verif_generation)
        gi = mxlGrainInfo()
        payload = _PU8()
        st = _lib.mxlFlowReaderGetGrain(
            self._h, index, timeout_ns, ctypes.byref(gi), ctypes.byref(payload))
        if st != MXL_STATUS_OK:
            return None
        self._auto_vu = True
        return index, gi, _np_view(payload, gi.grainSize)

    def get_slice(self, index, min_valid_slices, timeout_ns=100_000_000):
        """BLOQUANT (futex) — attend que le grain `index` ait AU MOINS `min_valid_slices`
        tranches valides (réveil à CHAQUE commit partiel du writer). Latence sous-trame :
        le consommateur traite la bande k dès qu'elle est publiée, sans attendre la trame.
        Renvoie (index, grainInfo, vue_numpy) — lire grainInfo.validSlices pour savoir
        combien de tranches sont réellement là — ou None si timeout. Requiert HAS_SLICES."""
        gi = mxlGrainInfo()
        payload = _PU8()
        st = _lib.mxlFlowReaderGetGrainSlice(
            self._h, index, int(min_valid_slices), timeout_ns,
            ctypes.byref(gi), ctypes.byref(payload))
        if st != MXL_STATUS_OK:
            return None
        return index, gi, _np_view(payload, gi.grainSize)

    def get_slice_nonblocking(self, index, min_valid_slices):
        """NON-BLOQUANT — le grain `index` s'il a ≥ min_valid_slices tranches valides, sinon
        None (TOO_EARLY compris). Même contrat de retour que get_slice(). Requiert HAS_SLICES."""
        gi = mxlGrainInfo()
        payload = _PU8()
        st = _lib.mxlFlowReaderGetGrainSliceNonBlocking(
            self._h, index, int(min_valid_slices), ctypes.byref(gi), ctypes.byref(payload))
        if st != MXL_STATUS_OK:
            return None
        return index, gi, _np_view(payload, gi.grainSize)

    # ------------------------------------------------------------------ GENLOCK (flux de réf maison)

    def _set_holdover(self, active, on_holdover):
        """Transition d'état holdover : ne signale (callback + flag public) qu'aux CHANGEMENTS
        (lock→holdover et retour), pas à chaque grain."""
        if bool(active) != self.holdover:
            self.holdover = bool(active)
            if on_holdover:
                try:
                    on_holdover(self.holdover)
                except Exception:
                    pass

    def _tick_holdover(self, period_ns):
        """Émet le prochain index de secours en respectant la cadence nominale (dort jusqu'à
        l'échéance monotone). Renvoie l'index (compteur interne)."""
        due = self._gl_hold_due_ns
        slp = due - time.monotonic_ns()
        if slp > 0:
            time.sleep(slp / 1e9)
        k = self._gl_hold_idx
        self._gl_hold_idx = k + 1
        self._gl_hold_due_ns = due + period_ns
        return k

    def wait_next_index(self, last_k, period_ns, timeout_ns=None,
                        poll_s=0.0002, on_holdover=None):
        """BLOQUE (poll léger de head_index) jusqu'à ce que le flux de référence avance à un
        NOUVEL index de grille (> `last_k`) ; renvoie cet index K (position de grille GM).

        C'est le cœur du genlock logiciel (docs/reference/PTP_CLOCK.md §4) : la réf est publiée par le moteur
        2110_io, un grain par époque PHC → chaque avance de son head est un tick de grille GM.
        Le producteur boucle : `k = ref.wait_next_index(last_k, period_ns); last_k = k;
        writer.open_grain(index=k)`. **Ne bloque JAMAIS indéfiniment** (holdover ci-dessous).

        Paramètres :
          last_k     : dernier index renvoyé (None au 1er appel → « snap » sur la tête courante,
                       ou amorçage free-run si la réf n'existe pas encore).
          period_ns  : période nominale d'un grain (= 1e9*fps_den/fps_num, cadence CHAMP si
                       entrelacé). Sert au HOLDOVER (cadence de secours) et au défaut de timeout.
          timeout_ns : délai sans avance de la réf au-delà duquel on bascule en HOLDOVER
                       (défaut ~2,5 périodes de grain, cf. « ~2-3 trames » du plan).
          poll_s     : pas de poll (défaut 200 µs — léger, sous une période de grain).
          on_holdover(active: bool) : callback optionnel appelé aux TRANSITIONS d'état
                       (True = entrée en holdover, False = retour au lock) pour que l'appelant
                       alerte. Aussi lisible à tout instant via `reader.holdover`.

        HOLDOVER (réf arrêtée : PHC perd le lock / moteur down) : après `timeout_ns` sans avance,
        on renvoie des index issus d'un compteur interne cadencé à `period_ns` (la sortie
        CONTINUE, cadence nominale) et on lève le flag/callback. Dès que la réf ré-avance, on
        « re-snap » sur son head (retour au lock, callback False).

        Cadence sous-multiple (producteur à /N vs réf) : NON géré ici — la réf tique à SA cadence.
        L'appelant divise (produire quand `k // N` change) OU s'abonne à une réf de sa propre
        cadence (un flux de réf par (domaine × cadence), cf. §4). Voir limitation dans le rapport.
        """
        period_ns = int(period_ns)
        if timeout_ns is None:
            timeout_ns = max(period_ns, int(period_ns * 2.5))

        # 1er appel : « snap » sur l'index de tête courant de la réf…
        if last_k is None:
            h = self.head_index()
            if h != MXL_UNDEFINED_INDEX:
                self._set_holdover(False, on_holdover)
                return int(h)
            last_k = -1   # …sinon réf absente au démarrage → on bascule en free-run ci-dessous

        # Déjà en holdover : cadencer au nominal tout en guettant le RETOUR de la réf.
        if self.holdover:
            due = self._gl_hold_due_ns
            while True:
                h = self.head_index()
                if h != MXL_UNDEFINED_INDEX and h > last_k:
                    self._set_holdover(False, on_holdover)
                    return int(h)
                now = time.monotonic_ns()
                if now >= due:
                    break
                time.sleep(min(poll_s, max(0.0, (due - now) / 1e9)))
            return self._tick_holdover(period_ns)

        # Locké : attendre que la réf avance, jusqu'au timeout.
        deadline = time.monotonic_ns() + timeout_ns
        while True:
            h = self.head_index()
            if h != MXL_UNDEFINED_INDEX and h > last_k:
                return int(h)
            if time.monotonic_ns() >= deadline:
                break
            time.sleep(poll_s)

        # Timeout → bascule HOLDOVER : reprend la numérotation juste après le dernier index,
        # échéance immédiate (la sortie continue sans hoquet), signale l'événement.
        self._gl_hold_idx = (last_k + 1) if last_k >= 0 else 0
        self._gl_hold_due_ns = time.monotonic_ns()
        self._set_holdover(True, on_holdover)
        return self._tick_holdover(period_ns)

    def close(self):
        if getattr(self, "_h", None):
            _lib.mxlReleaseFlowReader(self.inst._h, self._h)
            self._h = _VOID()
        self._close_own_instance()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# --------------------------------------------------------------------------- ANC (ST 2110-40)
# Codage du grain d'un flux DATA `video/smpte291`.
#
# ⚠ HISTORIQUE : jusqu'au 2026-07-12 on sérialisait un format MAISON
#   [u32 meta_num][u32 udw_fill][meta×16 o][udw : 1 o = 1 UDW]
# Il n'était compris QUE de nous. Banc croisé (cf. docs/reference/MXL_INTEROP.md) : un consommateur MXL stock
# parse ce grain comme du RFC 8331, en déduit « ANC count: 0 » et conclut SANS ERREUR que le flux
# ne porte aucun ANC → PERTE SILENCIEUSE du tally/timecode/sous-titres. Symétriquement, on ne
# savait pas lire l'ANC d'un tiers. Contrairement au planar (qui achète un vrai gain CPU sur des
# trames de plusieurs Mo), ce format maison n'achetait RIEN : un grain ANC fait 4 Ko.
# → Le format NORMATIF est désormais RFC 8331 (`ANC_RFC8331`), le maison n'est plus que lu
#   (`ANC_BOBI_V1`) le temps de la migration de la flotte.
#
# Layout RFC 8331 du grain (validé contre le parseur STOCK `mxl-data-probe`, gros-boutiste) :
#   [u16 Length][u8 ANC_Count][2 b F][22 b réservés]        ← en-tête 6 o (PAS d'ESN dans le grain)
#   puis, par paquet ANC, aligné 32 bits :
#     [1 b C][11 b Line_Number][12 b Horizontal_Offset][1 b S][7 b StreamNum]   (32 b)
#     puis un FLUX DE BITS de mots de 10 bits, MSB d'abord :
#       DID, SDID, Data_Count, UDW × Data_Count, Checksum_Word
#     puis bourrage de 0 jusqu'à l'alignement 32 bits.
# Chaque mot de 10 bits = 8 bits de données + b8 = parité paire + b9 = ~b8 (SMPTE 291).
# Nos UDW sont stockés sur 8 bits (comme libmtl : parité vérifiée puis jetée) → la conversion est
# SANS PERTE : parité et checksum se RECALCULENT.

ANC_RFC8331 = "rfc8331"      # normatif (interopérable)
ANC_BOBI_V1 = "bobi-v1"      # legacy maison — lecture seule (flotte en cours de migration)

# En-tête du grain = 6 octets (le grain MXL n'embarque PAS l'ESN, champ de niveau RTP) :
#   [u16 Length][u8 ANC_Count][2 b F + 6 b rsvd][u16 rsvd]
# Length = nombre d'octets de paquets ANC APRÈS l'en-tête. ⚠ Les paquets restent alignés sur
# 32 bits AU SENS DU RFC, c.-à-d. relativement au payload RTP (qui avait 2 octets d'ESN de plus)
# → dans le grain, ils démarrent à l'octet 6 puis tous les 4 octets (6, 10, 14…), soit des offsets
# de MOT IMPAIRS. C'est exactement ce qu'attend le parseur stock (`wordOffset % 2 == 0 → skip`).
_ANC_HDR = 6


def _anc_parity10(v8):
    """Mot 10 bits SMPTE 291 depuis 8 bits de données : b8 = parité PAIRE de b0-b7, b9 = ~b8."""
    v8 &= 0xFF
    p = bin(v8).count("1") & 1
    return v8 | (p << 8) | ((p ^ 1) << 9)


def _anc_checksum10(words10):
    """Checksum_Word SMPTE 291 : somme des 9 bits de poids faible de DID/SDID/DC/UDW, modulo 512,
    puis b9 = ~b8. `words10` = les mots 10 bits DÉJÀ paritaires, dans l'ordre."""
    s = sum(w & 0x1FF for w in words10) & 0x1FF
    return s | (((s >> 8) & 1) ^ 1) << 9


class _BitWriter:
    """Écriture d'un flux de bits MSB d'abord (ordre réseau du RFC 8331)."""
    def __init__(self):
        self.acc = 0
        self.n = 0
        self.out = bytearray()

    def write(self, value, bits):
        self.acc = (self.acc << bits) | (int(value) & ((1 << bits) - 1))
        self.n += bits
        while self.n >= 8:
            self.n -= 8
            self.out.append((self.acc >> self.n) & 0xFF)
        self.acc &= (1 << self.n) - 1

    def align32(self):
        """Bourrage de 0 jusqu'à l'alignement 32 bits (word_align du RFC)."""
        self.write(0, (8 - self.n) % 8)              # d'abord l'octet courant
        while len(self.out) % 4:
            self.out.append(0)


class _BitReader:
    """Lecture d'un flux de bits MSB d'abord. `base` = octet où commence le corps ANC : les
    alignements 32 bits sont RELATIFS à lui (cf. _ANC_HDR — le RFC aligne sur le payload RTP,
    qui portait 2 octets d'ESN de plus que le grain MXL → décalage de 2 octets)."""
    def __init__(self, buf, base=0):
        self.buf = buf
        self.base = base
        self.pos = base * 8

    def read(self, bits):
        v = 0
        for _ in range(bits):
            byte = self.pos >> 3
            if byte >= len(self.buf):
                raise ValueError("flux ANC tronqué")
            v = (v << 1) | ((self.buf[byte] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def align32(self):
        rel = self.pos - self.base * 8
        self.pos = self.base * 8 + ((rel + 31) & ~31)


def anc_pack_rfc8331(packets, field=0, grain_size=0):
    """Sérialise des paquets ANC en un grain RFC 8331 (→ lisible par un SDK MXL stock).

    `packets` : liste de dicts {did, sdid, udw (bytes/liste de données 8 bits),
                line (défaut 9), hori (0xFFF = indéfini), c (0 = luma), s, stream_num}.
    `field`   : F du RFC — 0b00 progressif/indéfini, 0b10 champ 1, 0b11 champ 2.
    `grain_size` : si > 0, le résultat est complété de zéros à cette taille (grain MXL fixe).
    Renvoie un ndarray uint8 prêt à écrire dans le grain.
    """
    w = _BitWriter()
    for p in packets:
        udw = p.get("udw") or b""
        if isinstance(udw, (bytes, bytearray, memoryview)):
            udw = list(bytes(udw))
        else:
            udw = [int(x) & 0xFF for x in udw]
        if len(udw) > 255:
            raise ValueError("Data_Count > 255 UDW")
        w.align32()                                    # chaque paquet démarre aligné 32 bits
        w.write(int(p.get("c", 0)) & 1, 1)             # C : canal chroma(1)/luma(0)
        w.write(int(p.get("line", 9)) & 0x7FF, 11)     # Line_Number
        w.write(int(p.get("hori", 0xFFF)) & 0xFFF, 12) # Horizontal_Offset (0xFFF = indéfini)
        w.write(int(p.get("s", 0)) & 1, 1)             # S : StreamNum significatif ?
        w.write(int(p.get("stream_num", 0)) & 0x7F, 7) # StreamNum (PERDU par l'ancien format !)
        # Flux de mots 10 bits : DID, SDID, DC, UDW…, Checksum.
        words = [_anc_parity10(int(p["did"])), _anc_parity10(int(p["sdid"])),
                 _anc_parity10(len(udw))] + [_anc_parity10(u) for u in udw]
        for wd in words:
            w.write(wd, 10)
        w.write(_anc_checksum10(words), 10)
        w.align32()                                    # word_align de fin de paquet
    body = bytes(w.out)                                # aligné 32 b RELATIVEMENT à son propre début

    hdr = bytearray(_ANC_HDR)
    hdr[0] = (len(body) >> 8) & 0xFF                   # Length (octets d'ANC après l'en-tête)
    hdr[1] = len(body) & 0xFF
    hdr[2] = len(packets) & 0xFF                       # ANC_Count
    hdr[3] = (int(field) & 0x3) << 6                   # F (2 b) + début des 22 b réservés
    out = bytes(hdr) + body
    if grain_size and len(out) < grain_size:
        out = out + b"\x00" * (grain_size - len(out))
    elif grain_size and len(out) > grain_size:
        raise ValueError("payload ANC (%d o) > grain (%d o)" % (len(out), grain_size))
    return np.frombuffer(out, dtype=np.uint8)


def anc_unpack_rfc8331(grain):
    """Décode un grain RFC 8331 → liste de paquets {did, sdid, udw(bytes 8 b), line, hori, c, s,
    stream_num, checksum_ok}. Tolérant : s'arrête proprement sur un grain tronqué/incohérent."""
    b = bytes(np.asarray(grain, dtype=np.uint8).tobytes()) if not isinstance(grain, (bytes, bytearray)) \
        else bytes(grain)
    if len(b) < _ANC_HDR:
        return []
    count = b[2]
    r = _BitReader(b, base=_ANC_HDR)     # alignements 32 b relatifs au début du corps
    out = []
    for _ in range(count):
        try:
            r.align32()
            c = r.read(1); line = r.read(11); hori = r.read(12)
            s = r.read(1); stream_num = r.read(7)
            did = r.read(10); sdid = r.read(10); dc = r.read(10)
            words = [did, sdid, dc]
            udw = []
            for _i in range(dc & 0xFF):
                v = r.read(10)
                words.append(v)
                udw.append(v & 0xFF)
            cs = r.read(10)
            out.append({
                "did": did & 0xFF, "sdid": sdid & 0xFF, "udw": bytes(udw),
                "line": line, "hori": hori, "c": c, "s": s, "stream_num": stream_num,
                "checksum_ok": (cs == _anc_checksum10(words)),
            })
        except ValueError:
            break                                      # grain tronqué → on rend ce qu'on a
    return out


def anc_unpack_bobi_v1(grain):
    """Décode l'ANCIEN grain MAISON [u32 meta_num][u32 udw_fill][meta×16 o][udw] — LECTURE SEULE
    (compat flotte en cours de migration ; ne plus produire ce format). Même sortie que
    anc_unpack_rfc8331 (`stream_num` absent de ce format → 0, `checksum_ok` inconnu → None)."""
    import struct as _struct
    b = bytes(np.asarray(grain, dtype=np.uint8).tobytes()) if not isinstance(grain, (bytes, bytearray)) \
        else bytes(grain)
    if len(b) < 8:
        return []
    meta_num, udw_fill = _struct.unpack_from("<II", b, 0)
    if meta_num == 0 or meta_num > 64:
        return []
    udw_base = 8 + meta_num * 16
    out = []
    for i in range(meta_num):
        try:
            did, sdid, line, hori, udw_size, udw_offset, c, s = _struct.unpack_from(
                "<8H", b, 8 + i * 16)
        except _struct.error:
            break
        a = udw_base + udw_offset
        if a + udw_size > len(b):
            break
        out.append({"did": did & 0xFF, "sdid": sdid & 0xFF, "udw": b[a:a + udw_size],
                    "line": line, "hori": hori, "c": c, "s": s, "stream_num": 0,
                    "checksum_ok": None})
    return out


def anc_format_of(flow_def):
    """Codage ANC déclaré par le producteur (champ `bobi_anc_format` du flowDef). Absent →
    ANC_BOBI_V1 (producteur pas encore migré). `flow_def` = dict (Instance.flow_def)."""
    if not isinstance(flow_def, dict):
        return ANC_BOBI_V1
    return flow_def.get("bobi_anc_format") or ANC_BOBI_V1


def anc_unpack(grain, flow_def=None):
    """Décodage AUTO : choisit le décodeur d'après le flowDef du producteur (flotte mixte)."""
    if anc_format_of(flow_def) == ANC_RFC8331:
        return anc_unpack_rfc8331(grain)
    return anc_unpack_bobi_v1(grain)


# ── ATC / timecode (SMPTE ST 12-1 · RP 188), DID 0x60 SDID 0x60 ────────────────────────────
def anc_atc_encode(hours, minutes, seconds, frames, drop_frame=False):
    """Timecode → les 16 UDW (8 bits) d'un paquet ATC, disposition LTC : UN CHIFFRE BCD PAR
    QUARTET, placé dans le quartet HAUT de son UDW. Les UDW d'index IMPAIR portent les drapeaux
    (ici 0). Correspondance :
        UDW[0]=unités d'images   UDW[2]=dizaines d'images (+ drop-frame en bit 2)
        UDW[4]=unités de sec.    UDW[6]=dizaines de sec.
        UDW[8]=unités de min.    UDW[10]=dizaines de min.
        UDW[12]=unités d'heures  UDW[14]=dizaines d'heures

    ⚠ Deux erreurs successives corrigées le 2026-08-07, toutes deux faute d'échantillons
    suffisants. On lisait d'abord les quartets BAS : timecode figé à 00:00:00:00, un bit qui
    clignotait. Puis, ayant vu la donnée dans les quartets HAUTS, on a recollé DEUX quartets en un
    octet et pris les dizaines dans le quartet haut de cet octet — ce qui perd les dizaines
    d'images : `10 08 20 00 50 00` se lisait 00:01:05:01 au lieu de 00:01:05:21. L'erreur était
    invisible tant que le compteur restait sous dix images. C'est un relevé sur un timecode qui
    FRANCHIT la dizaine qui a tranché."""
    q = [0] * 16
    q[0]  = frames % 10
    q[2]  = ((frames // 10) & 0x3) | (0x4 if drop_frame else 0)
    q[4]  = seconds % 10
    q[6]  = (seconds // 10) & 0x7
    q[8]  = minutes % 10
    q[10] = (minutes // 10) & 0x7
    q[12] = hours % 10
    q[14] = (hours // 10) & 0x3
    return bytes((v & 0x0F) << 4 for v in q)


def anc_atc_all(packets):
    """TOUS les ATC d'un grain, dans l'ordre — un flux en porte couramment DEUX (les saveurs
    ATC_LTC et ATC_VITC de ST 12-2), qui véhiculent le MÊME temps. Renvoie, DANS L'ORDRE,
    [{h, m, s, f, drop_frame, bas, tc}] — de quoi choisir lequel on incruste au lieu de subir le
    premier venu (cf. `quel` d'anc_atc_decode).

    `bas` = les quartets BAS bruts, non interprétés. Relevé en production le 2026-08-07 : les deux
    paquets d'un même grain ne diffèrent QUE là, d'un seul bit — mais ce bit CHANGE DE PLACE d'un
    grain à l'autre (UDW[0] ici, UDW[1] là). Ce n'est donc PAS un identifiant de saveur stable, et
    on se garde de le présenter comme tel : la sélection se fait par INDEX, qui est sûr. Le champ
    est exposé pour que qui saura l'interpréter puisse le faire."""
    out = []
    for p in packets:
        if p.get("did") != 0x60 or p.get("sdid") != 0x60:
            continue
        u = p.get("udw") or b""
        if len(u) < 16:
            continue
        # Un chiffre BCD par quartet HAUT (cf. anc_atc_encode) ; quartets BAS = drapeaux.
        q = [(x >> 4) & 0x0F for x in u[:16]]
        bas = bytes(x & 0x0F for x in u[:16])
        f = q[0] + (q[2] & 0x3) * 10
        df = bool((q[2] >> 2) & 1)
        sec = q[4] + (q[6] & 0x7) * 10
        m = q[8] + (q[10] & 0x7) * 10
        h = q[12] + (q[14] & 0x3) * 10
        out.append({"h": h, "m": m, "s": sec, "f": f, "drop_frame": df, "bas": bas,
                    "tc": "%02d:%02d:%02d%s%02d" % (h, m, sec, ";" if df else ":", f)})
    return out


def anc_atc_decode(packets, quel=0):
    """ATC d'une liste de paquets → (h, m, s, f, drop_frame) ou None. `quel` = index de la saveur
    à retenir quand le grain en porte plusieurs (0 = la première, comportement historique) ;
    au-delà du nombre disponible, on retombe sur la première. Voir `anc_atc_all` pour toutes les
    lire et choisir sur le marqueur."""
    tous = anc_atc_all(packets)
    if not tous:
        return None
    a = tous[quel] if 0 <= quel < len(tous) else tous[0]
    return (a["h"], a["m"], a["s"], a["f"], a["drop_frame"])


# ── Métadonnées ANC : registre DID/SDID + décodeurs des types utiles au monitoring ─────────
# Le codec ci-dessus rend TOUS les paquets (did/sdid/udw/checksum_ok), pas seulement l'ATC :
# ces helpers en tirent l'information exploitable par un mur de contrôle. Aucun n'est appelé
# automatiquement — un consommateur décode ce dont il a besoin.

# Registre SMPTE (ST 291-1) des types qu'on sait nommer. (did, sdid) → libellé court.
ANC_TYPES = {
    (0x60, 0x60): "ATC",         # SMPTE ST 12-2 — timecode auxiliaire (RP 188)
    (0x61, 0x01): "CC/708",      # SMPTE ST 334-1 — CDP (sous-titres CEA-708, 608 encapsulé)
    (0x61, 0x02): "CC/608",      # SMPTE ST 334-1 — CEA-608 direct
    (0x41, 0x01): "ST352",       # SMPTE ST 352 — identification de charge utile (payload ID)
    (0x41, 0x05): "AFD",         # SMPTE ST 2016-3 — format d'image actif + bar data
    (0x41, 0x07): "SCTE-104",    # SCTE-104 — déclencheurs (publicité/splice)
    (0x43, 0x02): "OP-47",       # OP-47 — télétexte/sous-titres (Europe)
    (0x44, 0x04): "KLV",         # SMPTE ST 336 — métadonnées KLV
    (0x45, 0x01): "Dolby",       # SMPTE ST 2020 — métadonnées audio Dolby
    (0x5F, 0xDC): "Camera",      # (usage constructeur courant) métadonnées caméra/optique
}


def anc_type_name(did, sdid):
    """Libellé court d'un type ANC, ou « DID:SDID » en hexa si inconnu (jamais None)."""
    return ANC_TYPES.get((int(did), int(sdid)), "%02X:%02X" % (int(did), int(sdid)))


def anc_inventory(packets):
    """INVENTAIRE d'un grain : ce que la source transporte réellement. Renvoie
    [{did, sdid, name, dc, checksum_ok}, …] — de quoi afficher « cette source porte un ATC, des
    sous-titres et un SCTE-104 », et repérer une métadonnée CORROMPUE (checksum_ok False)."""
    return [{"did": p["did"], "sdid": p["sdid"],
             "name": anc_type_name(p["did"], p["sdid"]),
             "dc": len(p.get("udw") or b""),
             "checksum_ok": p.get("checksum_ok")}
            for p in (packets or [])]


def anc_find(packets, did, sdid):
    """1er paquet du type demandé, ou None."""
    for p in (packets or []):
        if p.get("did") == did and p.get("sdid") == sdid:
            return p
    return None


# AFD (SMPTE ST 2016-3) : le 1er UDW porte le code AFD sur les bits 3-6, + le drapeau AR (bit 2).
_AFD_NAMES = {
    0b0000: "indéfini", 0b0010: "16:9 haut", 0b0011: "14:9 haut", 0b0100: "> 16:9 centré",
    0b1000: "plein cadre", 0b1001: "4:3 centré", 0b1010: "16:9 centré", 0b1011: "14:9 centré",
    0b1101: "4:3 (14:9 protégé)", 0b1110: "16:9 (14:9 protégé)", 0b1111: "16:9 (4:3 protégé)",
}


def anc_decode_afd(packets):
    """AFD → {code, label, aspect} ou None. `aspect` = rapport CODÉ du canal (4:3 ou 16:9)."""
    p = anc_find(packets, 0x41, 0x05)
    if not p or not p.get("udw"):
        return None
    b = p["udw"][0]
    code = (b >> 3) & 0x0F
    return {"code": code, "label": _AFD_NAMES.get(code, "AFD %d" % code),
            "aspect": "16:9" if (b >> 2) & 1 else "4:3"}


# ST 352 (payload ID) : 4 UDW = 4 octets. Byte 0 = version/structure, byte 1 = balayage +
# fréquence image, byte 2 = échantillonnage/profondeur, byte 3 = colorimétrie/dynamique.
_ST352_RATE = {0x2: "24/1.001", 0x3: "24", 0x4: "47.95", 0x5: "25", 0x6: "29.97", 0x7: "30",
               0x8: "48/1.001", 0x9: "48", 0xA: "50", 0xB: "59.94", 0xC: "60"}


def anc_decode_st352(packets):
    """Identification de charge utile ST 352 → {scan, rate, label} ou None.
    Le format DÉCLARÉ PAR LE SIGNAL — à confronter au SDP (détecteur de désaccord)."""
    p = anc_find(packets, 0x41, 0x01)
    if not p or len(p.get("udw") or b"") < 4:
        return None
    b1 = p["udw"][1]
    scan = "p" if (b1 >> 6) & 1 else "i"          # bit 6 : progressif (1) / entrelacé (0)
    rate = _ST352_RATE.get(b1 & 0x0F, "?")
    return {"scan": scan, "rate": rate, "label": "%s%s" % (rate, scan)}


def anc_scte104(packets):
    """SCTE-104 présent → {op_id} (opération demandée), sinon None. Ne décode PAS la charge
    utile complète : un mur de contrôle veut savoir QU'UN déclencheur est passé, et quand."""
    p = anc_find(packets, 0x41, 0x07)
    if not p or len(p.get("udw") or b"") < 4:
        return None
    u = p["udw"]
    return {"op_id": (u[2] << 8) | u[3]}          # multiple_operation_message : opID sur 16 b


def anc_captions(packets):
    """SOUS-TITRES : présence + texte CEA-608 si décodable. Renvoie
    {present, kind, cc608} ou None si la source n'en porte pas.

    ST 334-1 encapsule les sous-titres dans un CDP (Caption Distribution Packet) : en-tête
    0x9669, puis des sections ; la section `ccdata` (0x72) porte des triplets
    (cc_valid/cc_type, byte1, byte2). cc_type 0/1 = CEA-608 champ 1/2 (caractères directement
    lisibles) ; cc_type 2/3 = paquets DTVCC (CEA-708) — leur RÉASSEMBLAGE en texte est un
    chantier à part (on se contente de signaler leur présence)."""
    p = anc_find(packets, 0x61, 0x01) or anc_find(packets, 0x61, 0x02)
    if not p:
        return None
    u = p.get("udw") or b""
    out = {"present": True, "kind": anc_type_name(p["did"], p["sdid"]),
           "cc608": "", "dtvcc": False}
    # Localiser la section ccdata du CDP (0x72), après l'en-tête 0x9669.
    i = 0
    if len(u) >= 2 and u[0] == 0x96 and u[1] == 0x69:
        i = 7                                     # 0x9669 + len + rate + flags + counter
        while i < len(u):
            sec = u[i]
            if sec == 0x72:                       # ccdata_section
                cc_count = u[i + 1] & 0x1F if i + 1 < len(u) else 0
                j = i + 2
                chars = []
                for _k in range(cc_count):
                    if j + 2 >= len(u):
                        break
                    cc_valid = (u[j] >> 2) & 1
                    cc_type = u[j] & 0x03
                    b1, b2 = u[j + 1] & 0x7F, u[j + 2] & 0x7F   # bit 7 = parité
                    if cc_valid and cc_type in (0, 1):
                        for ch in (b1, b2):
                            if 0x20 <= ch < 0x7F:
                                chars.append(chr(ch))
                    elif cc_valid and cc_type in (2, 3):
                        out["dtvcc"] = True
                    j += 3
                out["cc608"] = "".join(chars).strip()
                break
            if sec in (0x71, 0x73):               # sections de longueur variable → on s'arrête
                break
            i += 1
    return out



# --------------------------------------------------------------------------- OP-47 / télétexte

# HAMMING 8/4 (ETS 300 706 § 8.2). Les positions sont celles de la norme : en partant du bit de
# poids faible, P1 D? P3 D? … — quatre bits de donnée (D1..D4) entrelacés avec quatre bits de
# protection (P1..P4). La TABLE est construite ICI à partir de ces positions plutôt que recopiée :
# une table de 256 entrées recopiée à la main est une source d'erreurs qu'aucune relecture ne
# rattrape, et celle-ci se vérifie par aller-retour.
_H84_D = (2, 4, 5, 6)          # positions des bits de donnée D1..D4
_H84_P = (0, 1, 3, 7)          # positions des bits de protection P1..P4
# Chaque bit de protection couvre un sous-ensemble des bits de donnée (équations de la norme).
_H84_COUV = ((0, 1, 3), (0, 2, 3), (1, 2, 3), (0, 1, 2, 3))


def _h84_encode(v4):
    """4 bits → octet protégé Hamming 8/4."""
    v4 &= 0x0F
    o = 0
    for i, pos in enumerate(_H84_D):
        if (v4 >> i) & 1:
            o |= 1 << pos
    for i, pos in enumerate(_H84_P):
        par = 0
        for d in _H84_COUV[i]:
            par ^= (v4 >> d) & 1
        # P4 est une parité PAIRE sur l'octet entier ; P1..P3 sont des parités IMPAIRES sur
        # leur sous-ensemble. C'est la convention de la norme, et l'aller-retour la vérifie.
        if i < 3:
            par ^= 1
        if par:
            o |= 1 << pos
    return o


_H84_TABLE = [None] * 256
for _v in range(16):
    _H84_TABLE[_h84_encode(_v)] = _v
# CORRECTION D'UNE ERREUR SIMPLE : c'est tout l'objet d'un code de Hamming, et la sauter
# reviendrait à jeter des lignes qu'un décodeur conforme lirait. Chaque octet à distance 1 d'un
# mot valide est rattaché à ce mot — s'il l'est de DEUX mots valides, on refuse (double erreur
# détectée, non corrigible).
for _v in range(16):
    _mot = _h84_encode(_v)
    for _b in range(8):
        _abime = _mot ^ (1 << _b)
        if _H84_TABLE[_abime] is None:
            _H84_TABLE[_abime] = _v
        elif _H84_TABLE[_abime] != _v:
            _H84_TABLE[_abime] = None          # ambigu → non corrigible
del _v, _mot, _b, _abime


def hamming84(octet):
    """Octet → 4 bits de donnée, avec correction d'une erreur simple. None si non corrigible."""
    return _H84_TABLE[octet & 0xFF]


def _parite_impaire_ok(o):
    """Les octets de TEXTE télétexte portent une parité IMPAIRE sur le bit 7."""
    v = o
    c = 0
    while v:
        c ^= v & 1
        v >>= 1
    return c == 1


# Jeu G0 latin (ETS 300 706) : les codes 0x20-0x7F sont l'ASCII, à quelques positions près qui
# dépendent de l'option nationale. On rend l'ASCII tel quel et on remplace les commandes par des
# espaces — un mur de contrôle veut LIRE le sous-titre, pas restituer une page télétexte exacte.
def _texte_g0(octets):
    out = []
    for o in octets:
        c = o & 0x7F
        out.append(chr(c) if 0x20 <= c < 0x7F else " ")
    return "".join(out)


def teletext_ligne(bloc42):
    """Une ligne WST de 42 octets → {magazine, paquet, texte, parite_ok} ou None.

    Les deux premiers octets sont protégés en Hamming 8/4 et portent l'adresse : 3 bits de
    magazine et 5 bits de numéro de paquet. Les 40 suivants sont du texte en parité impaire.
    Le paquet 0 est un EN-TÊTE de page : ses huit premiers octets de charge utile sont des
    données de page (numéro, contrôle) et non du texte — on ne rend donc que la fin."""
    if not bloc42 or len(bloc42) < 2:
        return None
    a, b = hamming84(bloc42[0]), hamming84(bloc42[1])
    if a is None or b is None:
        return None
    magazine = a & 0x07
    paquet = ((a >> 3) & 1) | (b << 1)
    data = bloc42[2:42]
    mauvais = sum(0 if _parite_impaire_ok(o) else 1 for o in data)
    txt = _texte_g0(data[8:] if paquet == 0 else data)
    return {"magazine": magazine or 8, "paquet": paquet, "texte": txt.rstrip(),
            "parite_ok": mauvais == 0, "octets_douteux": mauvais}


# Code de TRAME d'une ligne WST : il ouvre chaque bloc de données d'un paquet OP-47.
OP47_FRAMING = 0xE4
OP47_BLOC = 45          # code de trame + 2 octets d'adresse + 42 octets de ligne… voir ci-dessous


def anc_decode_op47(packets):
    """OP-47 (SMPTE RDD 8) : sous-titres télétexte transportés en ANC → lignes décodées.

    Renvoie {present, lignes: [{magazine, paquet, texte, …}], texte} ou None si la source n'en
    porte pas.

    ⚠ CE DÉCODEUR LOCALISE LES BLOCS PAR LEUR CODE DE TRAME (0xE4), PAS PAR UN DÉCALAGE.
    C'est une décision, et elle vient d'une limite assumée : la structure exacte de l'en-tête du
    paquet OP-47 (largeur des descripteurs de ligne) n'a pas pu être vérifiée sur la norme depuis
    cette machine. Plutôt que de supposer un décalage — un décodeur qui se trompe d'un octet rend
    du texte FAUX, et personne ne le voit —, on cherche le motif que la norme impose au début de
    chaque ligne, puis on VALIDE : les deux octets d'adresse doivent passer le Hamming 8/4, et
    les 40 octets de texte leur parité impaire. Un bloc qui ne valide pas est jeté.

    Conséquence à connaître : les NUMÉROS DE LIGNE VBI (portés par les descripteurs d'en-tête) ne
    sont pas rendus. Le texte, le magazine et le numéro de paquet le sont — c'est ce qu'on lit.
    """
    trouves = [p for p in (packets or []) if p.get("did") == 0x43 and p.get("sdid") == 0x02]
    if not trouves:
        return None
    lignes = []
    for p in trouves:
        u = p.get("udw") or b""
        i = 0
        while i < len(u):
            if u[i] != OP47_FRAMING:
                i += 1
                continue
            bloc = u[i + 1:i + 1 + 42]
            l = teletext_ligne(bloc) if len(bloc) == 42 else None
            if l and (l["parite_ok"] or l["octets_douteux"] <= 4):
                lignes.append(l)
                i += 43
            else:
                i += 1
    # Les lignes de SOUS-TITRE sont les paquets 1 à 23 (le 0 est l'en-tête de page). On les rend
    # dans l'ordre reçu : c'est l'ordre d'affichage.
    texte = " ".join(l["texte"].strip() for l in lignes
                     if 1 <= l["paquet"] <= 23 and l["texte"].strip())
    return {"present": True, "lignes": lignes, "texte": texte.strip()}


def op47_encode_lignes(lignes, magazine=8, base_paquet=1):
    """Fabrique une charge utile OP-47 MINIMALE à partir de textes — pour les bancs et les
    auto-contrôles. Elle n'est PAS destinée à l'émission : elle ne pose ni descripteurs, ni
    compteur de séquence, ni checksum de paquet (cf. la limite documentée dans
    `anc_decode_op47`). Elle sert à prouver que la chaîne de décodage lit bien ce que la norme
    impose au niveau des LIGNES."""
    out = bytearray(b"\x51\x15\x00\x02")
    for k, txt in enumerate(lignes):
        paquet = base_paquet + k
        a = _h84_encode((magazine & 0x07) | ((paquet & 1) << 3))
        b = _h84_encode((paquet >> 1) & 0x0F)
        data = bytearray()
        for o in (txt.encode("ascii", "replace")[:40].ljust(40, b" ")):
            data.append(o if _parite_impaire_ok(o) else (o | 0x80))
        out.append(OP47_FRAMING)
        out += bytes((a, b)) + data
    out += b"\x74\x00\x00\x00"
    return bytes(out)

# --------------------------------------------------------------------------- AUDIO (samples continus)

def build_data_flow_def(name, fps_num=25, fps_den=1, label=None, anc_format=ANC_RFC8331):
    """flowDef d'un flux DATA (ANC ST 2110-40, media_type video/smpte291). Le grain = une payload
    ANC (taille DATA_FORMAT_GRAIN_SIZE fixée par MXL). Écrire via Writer(flow_def=build_data_flow_def
    (name)) ; lire via Reader.get_latest() (bytes du grain) → décodage ANC côté consommateur.

    `anc_format` (champ NON standard `bobi_anc_format`, ignoré par un SDK stock — même vecteur que
    `slice_height`) déclare le codage du grain :
      - "rfc8331" (DÉFAUT depuis 2026-07-12) : conforme RFC 8331 → lisible par un tiers ;
      - "bobi-v1" : ancienne sérialisation maison [u32 meta_num][u32 udw_fill][meta×16][udw].
    Un consommateur lit ce champ (`anc_format_of()`) et choisit son décodeur → flotte MIXTE
    supportée pendant la migration (un producteur pas encore mis à jour n'annonce rien → bobi-v1).
    """
    return json.dumps({
        "id": flow_id(name),
        "bobi_anc_format": anc_format,
        "tags": {"urn:x-nmos:tag:grouphint/v1.0": ["%s:Data" % (label or name)]},
        "format": "urn:x-nmos:format:data",
        "label": label or name,
        "media_type": "video/smpte291",
        "grain_rate": {"numerator": int(fps_num), "denominator": int(fps_den)},
    })


def build_audio_flow_def(name, channels=8, sample_rate=48000, label=None):
    """flowDef d'un flux AUDIO continu (float32 par canal). sample_rate en Hz."""
    return json.dumps({
        "id": flow_id(name),
        "tags": {"urn:x-nmos:tag:grouphint/v1.0": ["%s:Audio" % (label or name)]},
        "format": "urn:x-nmos:format:audio",
        "label": label or name,
        "media_type": "audio/float32",
        "sample_rate": {"numerator": int(sample_rate), "denominator": 1},
        "channel_count": int(channels),
        "bit_depth": 32,
    })


def _audio_planes(slc, n_frames):
    """Itère (canal, np.float32 view écrivable/lisible) sur une slice multi-buffer (2 fragments,
    wrap d'anneau). Renvoie une liste de (c, [(view, n)]) — fragments non vides par canal."""
    ch = int(slc.count); stride = int(slc.stride)
    base = slc.base
    out = []
    for c in range(ch):
        frags = []
        for fi in range(2):
            f = base.fragments[fi]
            if not f.size:
                continue
            cnt = int(f.size) // 4   # float32
            addr = int(f.pointer) + c * stride
            buf = (ctypes.c_float * cnt).from_address(addr)
            frags.append((np.frombuffer(buf, dtype=np.float32), cnt))
        out.append((c, frags))
    return out


class AudioWriter:
    """Producteur audio MXL (samples float32 par canal). `write(arr)` où arr = (n_samples, channels)."""

    def __init__(self, instance, name, channels=8, sample_rate=48000, index_mode="tai"):
        _load()
        self.inst = instance; self.name = name
        self.channels = int(channels); self.sample_rate = int(sample_rate)
        self.flow_def = build_audio_flow_def(name, channels, sample_rate)
        self.rate = mxlRational(int(sample_rate), 1)
        self.index_mode = index_mode
        self._counter = 0
        self._h = _VOID(); created = ctypes.c_bool(False)
        _ck("mxlCreateFlowWriter", _lib.mxlCreateFlowWriter(
            instance._h, self.flow_def.encode(), _flow_options(),
            ctypes.byref(self._h), None, ctypes.byref(created)))
        self.created = bool(created.value)


    def index_time_ns(self, index):
        """Instant TAI (ns) de l'index d'ÉCHANTILLON `index`, à la cadence de ce flux.

        ⚠ L'index audio MXL est ONE-PAST-THE-END : `index` désigne les échantillons
        `[index - count, index)`. L'instant rendu est donc celui de la FIN du bloc. La convention
        étant la même des deux côtés, une DIFFÉRENCE reste juste — c'est l'instant absolu qui doit
        se lire comme une fin. Cf. [[mxl-audio-index-is-one-past-the-end]].
        """
        return int(_lib.mxlIndexToTimestamp(ctypes.byref(self.rate), int(index)))
    def write(self, samples, index=None):
        """samples : float32 (n, channels) (ou (n,) mono). `index` = index du PREMIER sample écrit
        (convention de CE binding) ; renvoie ce même index de départ.

        ⚠ L'API C, elle, prend un index ONE-PAST-THE-END : `mxlFlowWriterOpenSamples(index, count)`
        écrit les `count` samples aux indices **[index - count, index)** — le paramètre est un index
        de TÊTE, pas de départ (cf. « The head index of the samples that will be mutated » dans
        flow.h). La conversion se fait ICI, à la frontière ctypes, pour que tous les appelants
        raisonnent en index de début. Le binding Rust d'upstream avait fait la confusion inverse :
        producteur par blocs de 1024 contre consommateur par lots de 48 → audio 20 ms devant son
        PTS (correctif amont 31b44804, MXL v1.1.0-rc1)."""
        arr = np.ascontiguousarray(samples, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        n = int(arr.shape[0])
        if index is not None:
            idx = index
        elif self.index_mode == "tai":
            idx = _lib.mxlGetCurrentIndex(ctypes.byref(self.rate)) - n   # bloc se terminant « maintenant »
        else:
            idx = self._counter
        slc = mxlMutableWrappedMultiBufferSlice()
        _ck("mxlFlowWriterOpenSamples",
            _lib.mxlFlowWriterOpenSamples(self._h, idx + n, n, ctypes.byref(slc)))
        for c, frags in _audio_planes(slc, n):
            pos = 0
            for view, cnt in frags:
                view[:] = arr[pos:pos + cnt, c]
                pos += cnt
        _ck("mxlFlowWriterCommitSamples", _lib.mxlFlowWriterCommitSamples(self._h))
        self._counter += n
        return idx

    def close(self):
        if getattr(self, "_h", None):
            _lib.mxlReleaseFlowWriter(self.inst._h, self._h); self._h = _VOID()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class AudioReader(_StaleGuard):
    """Consommateur audio MXL. `read_latest(count)` → float32 (count, channels) ou None."""

    def __init__(self, instance, name):
        _load()
        self.inst = instance; self.name = name; self.fid = flow_id(name)
        self._opts = None
        self._own_inst = None
        self._stale_n = 0
        self._head_seen = None      # (head, instant monotone) — détection de décrochage audio
        self._rate = None           # cadence d'échantillonnage, résolue à la 1re conversion
        self._h = _VOID()
        _ck("mxlCreateFlowReader", _lib.mxlCreateFlowReader(
            instance._h, self.fid.encode(), None, ctypes.byref(self._h)))

    def index_time_ns(self, index):
        """Instant TAI (ns) de l'index d'ÉCHANTILLON `index` — pendant du AudioWriter, côté lecture.

        Rend **None** si la cadence n'est pas lisible (flux absent du domaine) : jamais 0, qui se
        lirait comme un instant valide. Même contrat que `Reader.index_time_ns`, ce qui rend
        `StageDelay` utilisable tel quel sur une chaîne AUDIO.

        ⚠ One-past-the-end, cf. `AudioWriter.index_time_ns`.
        """
        if self._rate is None:
            fd = self.inst.flow_def(fid=self.fid)
            if not fd:
                return None
            sr = (fd.get("sample_rate") or {})
            try:
                num, den = int(sr.get("numerator")), int(sr.get("denominator", 1))
            except (TypeError, ValueError):
                return None
            if num <= 0 or den <= 0:
                return None
            self._rate = mxlRational(num, den)
        return int(_lib.mxlIndexToTimestamp(ctypes.byref(self._rate), int(index)))

    def _verif_generation(self):
        return self.reopen_if_head_stale(on_reopen=self._auto_trace)

    def reopen_if_head_stale(self, max_s=5.0, on_reopen=None):
        """Pendant audio de `reopen_if_stale` : les writers audio ne bumpent PAS lastWriteTime, le
        seul signe de décrochage est un `head_index` qui ne bouge plus. ⚠ Un flux réellement MUET a
        un head qui AVANCE (le writer comble en silence) : un head figé n'est donc pas « du
        silence », c'est un lecteur décroché (ou un producteur mort — rouvrir est bon dans les deux
        cas). Renvoie True si reconnecté."""
        now = time.monotonic()
        try:
            head = int(self.head_index())
        except Exception:
            head = -1
        prev = self._head_seen
        if prev is None or head != prev[0]:
            self._head_seen = (head, now)
            if prev is not None:
                self._stale_n = 0       # head qui avance → lecteur sain, escalade désarmée
            return False
        if now - prev[1] <= max_s:
            return False
        n = self.reopen()
        self._head_seen = None
        if on_reopen is not None:
            try: on_reopen(now - prev[1], n)
            except Exception: pass
        return True

    def head_index(self):
        rt = mxlFlowRuntimeInfo()
        _ck("mxlFlowReaderGetRuntimeInfo",
            _lib.mxlFlowReaderGetRuntimeInfo(self._h, ctypes.byref(rt)))
        return rt.headIndex

    def last_write_time(self):
        """Instant TAI (ns) de la dernière écriture du producteur — pour la latence transit
        (now_tai() - last_write_time())/1e6 ms. 0 si indispo. (Miroir de Reader.last_write_time.)"""
        rt = mxlFlowRuntimeInfo()
        st = _lib.mxlFlowReaderGetRuntimeInfo(self._h, ctypes.byref(rt))
        return rt.lastWriteTime if st == MXL_STATUS_OK else 0

    def read_from(self, start_index, count):
        """Lecture NON-bloquante des `count` samples **[start_index, start_index + count)** →
        (count, channels) float32, ou None si indisponible (trop tôt/tard ou flux absent). Permet
        une consommation SÉQUENTIELLE sans trou (encodeurs streamer/recorder : suivre sa position
        de lecture et avancer de `count`), là où `read_latest` ne sert qu'à une fenêtre récente (VU).

        ⚠ Même conversion que `AudioWriter.write` : l'API C prend un index ONE-PAST-THE-END et rend
        **[index - count, index)**. On lui passe donc `start_index + count`. Sans ce `+ count`,
        cette fonction rendait le bloc PRÉCÉDANT celui que son nom annonce."""
        # Côté audio le signe du décrochage est un head FIGÉ (les writers audio ne bumpent pas
        # lastWriteTime) — cf. reopen_if_head_stale. Un flux réellement muet a un head qui AVANCE.
        self._auto_garde(self._verif_generation)
        slc = mxlWrappedMultiBufferSlice()
        st = _lib.mxlFlowReaderGetSamplesNonBlocking(self._h, start_index + count, count,
                                                     ctypes.byref(slc))
        if st != MXL_STATUS_OK:
            return None
        self._auto_vu = True
        ch = int(slc.count)
        out = np.empty((count, ch), dtype=np.float32)
        for c, frags in _audio_planes(slc, count):
            pos = 0
            for view, cnt in frags:
                out[pos:pos + cnt, c] = view
                pos += cnt
        return out

    def read_latest(self, count):
        """Derniers `count` samples disponibles → (count, channels) float32, ou None.

        `headIndex` est un index ONE-PAST-THE-END (la position d'écriture, pas le début du dernier
        bloc — l'ancien commentaire disait l'inverse). Les derniers samples sont donc
        **[head - count, head)**. Ce `- count` est le pendant OBLIGATOIRE du `+ count` de
        `read_from` : sans lui on demanderait une fenêtre encore À ÉCRIRE, et les VU-mètres du
        multiview ne rendraient plus que None."""
        self._auto_garde(self._verif_generation)
        head = self.head_index()
        if head == MXL_UNDEFINED_INDEX or head < count:
            return None
        return self.read_from(head - count, count)

    def close(self):
        if getattr(self, "_h", None):
            _lib.mxlReleaseFlowReader(self.inst._h, self._h); self._h = _VOID()
        self._close_own_instance()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def sleep_until_tai(deadline_ns):
    """Dort jusqu'à l'instant TAI absolu `deadline_ns`. Renvoie le retard (ns) si l'échéance était
    déjà passée, 0 sinon.

    Le reste est recalculé depuis l'horloge à chaque appel : la grille ABSOLUE tient, la gigue d'un
    tick est corrigée au suivant, pas de dérive cumulée. ⚠ L'échéance se compare à `now_tai()`
    (CLOCK_TAI), JAMAIS à CLOCK_REALTIME : sur un nœud dont l'horloge système porte l'UTC, mélanger
    les deux décale la cadence de tout l'offset TAI-UTC."""
    reste = int(deadline_ns) - now_tai()
    if reste > 0:
        time.sleep(reste / 1e9)
        return 0
    return -reste


def now_tai():
    """Horloge TAI courante (ns, epoch ST 2059) — même base que Reader.last_write_time()."""
    _load()
    return _lib.mxlGetTime()


def current_index(fps_num, fps_den=1):
    """Index de grain COURANT sur la grille TAI ST 2059, pour la cadence donnée.

    C'est la coordonnée temporelle de l'instant présent dans l'espace d'index d'un flux à cette
    cadence — la même que celle qu'un Writer en mode « tai » attribuerait à un grain écrit
    maintenant.

    À quoi ça sert : mesurer l'ÂGE d'un contenu. Comparer l'index d'un grain LU à un index
    incrusté dans l'image ne mesure pas un âge — un étage qui PROPAGE la coordonnée source
    (le plugin `delay` le fait, à dessein) déplace les deux termes ensemble et la différence
    reste nulle, alors que le contenu a bel et bien vieilli. Comparer à l'index de MAINTENANT
    ne dépend, lui, d'aucune convention d'étage. Cf. docs/reference/LATENCE_CHAINE.md et la
    règle « âge absolu contre horloge TAI ».
    """
    _load()
    r = mxlRational()
    r.numerator, r.denominator = int(fps_num), int(fps_den or 1)
    return int(_lib.mxlGetCurrentIndex(ctypes.byref(r)))


def lib_version():
    _load()
    v = mxlVersionType()
    _ck("mxlGetVersion", _lib.mxlGetVersion(ctypes.byref(v)))
    return (v.major, v.minor, v.bugfix, v.build,
            v.full.decode() if v.full else "")


# --------------------------------------------------------------------------- v210 (interop MXL)
# Pack/unpack v210 (4:2:2 10 bits, seul type vidéo du SDK MXL stock) ↔ notre planar contigu
# (plans Y,Cb,Cr — cf. frame_bytes). Chemin rapide = libbobi_v210.so (C auto-vectorisé, ~4-5 ms
# l'aller-retour 1080p, buildée dans les images runtime) ; repli numpy bit-exact sinon (~30 ms,
# vieilles images). Sert au pont de frontière inter-éditeurs (flow v210 miroir, docs/reference/MXL_INTEROP.md).

_v210_lib = None            # CDLL chargée, ou False si introuvable (repli numpy)


def v210_stride(width):
    """Stride d'une ligne v210 (octets) : groupes de 48 px alignés 128 o."""
    return ((int(width) + 47) // 48) * 128


def v210_frame_bytes(width, height):
    """Taille d'une frame v210 (octets) = stride × hauteur (grainSize du flow v210)."""
    return v210_stride(width) * int(height)


# ------------------------------------------------------------------ écriture non temporelle
# Écrire un grain, c'est déverser une trame linéaire dans de la tmpfs que le producteur ne relit
# JAMAIS. En magasins normaux le CPU LIT d'abord chaque ligne de cache qu'il va écraser
# (read-for-ownership) → ~2× le trafic mémoire. Les magasins en flux (`movntdq`) le suppriment.
#
# ⚠ CE QUI A ÉTÉ MESURÉ, ET CE QUI A ÉTÉ DÉMENTI (2026-08-02, bancs script_templates/nt_*.py).
# Première conclusion, FAUSSE : « la glibc bascule au-delà de x86_non_temporal_threshold, donc
# sous ce seuil il faut le faire nous-mêmes ». Vérifiée dans les conteneurs de production sur la
# MÊME trame de 4 Mo :
#     dell-1  memcpy 411 µs → non temporel 321 µs   −22 %
#     dl360-1 memcpy 323 µs → non temporel 390 µs   +20 %   ← le seuil y prédisait le PLUS gros gain
# Le seuil glibc décrit ce que FAIT la glibc, pas ce qui est RAPIDE sur la machine. Aucune règle
# statique ne marche : dl360-1 (35,8 Mio de L3, mono-socket) écrit déjà à 12,8 Go/s en memcpy.
#
# D'où : on ne DÉDUIT plus, on MESURE, une fois par processus, sur le nœud réel et à la taille
# réelle (`_nt_calibrer`). Le chemin le plus rapide gagne — donc pas de régression possible, au
# pire on retombe sur `memcpy`.
#
# NE PAS remplacer ceci par `GLIBC_TUNABLES` sur le conteneur : ce seuil est un réglage de
# PROCESSUS, il s'appliquerait AUSSI à la copie d'ENTRÉE d'un consommateur (buffer de travail relu
# aussitôt, donc éjecté du cache pour rien) — mesuré +9 % à +86 % de CPU. Le ciblage sur la seule
# écriture du grain gagne dans les deux régimes.
#
# NE PAS porter ceci dans les kernels de composition (mvcompose.c) : ils écrivent des TUILES, et
# quand la largeur de tuile n'est pas multiple de 64 o la dernière ligne de chaque rangée est
# partagée avec la tuile voisine → ligne partielle mêlée à des magasins en flux → **3,5 à 5× plus
# LENT**. Mur 4×4 en 1080p 8 bits = tuiles de 480 o : pile le cas catastrophique.

_nt_lib = None                  # CDLL chargée, ou False si introuvable (repli memcpy)
_nt_verdict = {}                # taille → True si le non temporel a GAGNÉ la calibration ici
NT_MIN = int(os.environ.get("BOBI_MXL_NT_MIN", 2 * 1024 * 1024))
NT_CAL_GRAINS = 10              # anneau de calibration : l'ensemble de travail doit sortir du L3,
                                # sinon la calibration conclut l'inverse de la réalité (piège vécu)
NT_CAL_ITERS = 20
NT_ENABLED = os.environ.get("BOBI_MXL_NT", "1") not in ("0", "no", "off")


def _nt_load():
    """Charge libbobi_nt. None → aucun non temporel, tout passe en memcpy (comportement d'avant,
    aucune régression possible si le .so n'est pas dans l'image)."""
    global _nt_lib
    if _nt_lib is not None:
        return _nt_lib or None
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("BOBI_NT_LIB")]
    for d in ("/usr/local/lib", here):
        cands.append(os.path.join(d, "libbobi_nt.so"))
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
        except OSError:
            continue
        lib.bobi_copy_nt.restype = None
        lib.bobi_copy_nt.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        lib.bobi_nt_has_avx2.restype = ctypes.c_int
        _nt_lib = lib
        return lib
    _nt_lib = False
    return None


def _nt_calibrer(lib, n):
    """MESURE les deux chemins sur CE nœud, à CETTE taille, et renvoie True si le non temporel
    gagne. Anneau de NT_CAL_GRAINS grains (~40 Mo à 1080p) : écrire toujours le même buffer le
    laisserait résident en L3 et inverserait le verdict. Coût ~15 ms, une fois par processus et
    par taille. Toute erreur → False (on garde memcpy)."""
    try:
        import mmap as _mm
        total = n * NT_CAL_GRAINS
        buf = _mm.mmap(-1, total)
        dst = np.frombuffer(buf, dtype=np.uint8)
        dst[:] = 0                                   # pré-faute : ne pas mesurer l'allocation
        src = np.empty(n, dtype=np.uint8)
        a_src = src.ctypes.data

        def _chrono(nt):
            t0 = time.perf_counter()
            for i in range(NT_CAL_ITERS):
                off = (i % NT_CAL_GRAINS) * n
                if nt:
                    lib.bobi_copy_nt(dst.ctypes.data + off, a_src, n)
                else:
                    dst[off:off + n] = src
            return time.perf_counter() - t0

        _chrono(False); _chrono(True)                # chauffe des deux côtés
        t_plain, t_nt = _chrono(False), _chrono(True)
        verdict = t_nt < t_plain * 0.97              # marge : ne basculer que sur un gain NET
    except Exception:
        return False
    finally:
        # Libérer APRÈS le verdict, et sans jamais le compromettre : tant qu'une vue numpy
        # référence le mmap, `close()` lève BufferError. Placée avant le calcul, cette ligne
        # faisait retomber une mesure JUSTE dans l'`except` → verdict False sur TOUS les nœuds,
        # y compris ceux où le non temporel gagnait de 46 %. Échec silencieux vécu.
        try:
            del dst, src
            buf.close()
        except Exception:
            pass
    return verdict


def nt_status():
    """État du chemin non temporel — diagnostic (« pourquoi pas de gain ici ? »)."""
    lib = _nt_load()
    return {"available": bool(lib), "enabled": bool(lib) and NT_ENABLED,
            "nt_min": NT_MIN, "verdicts": dict(_nt_verdict),
            "avx2": bool(lib and lib.bobi_nt_has_avx2())}


def blit(dst, src):
    """Écrit `src` dans la vue de grain `dst` — le remplaçant de `vw[:n] = src`.

    Magasins non temporels SI la calibration les a trouvés plus rapides sur ce nœud, sinon
    `memcpy`. `.so` absent, taille sous NT_MIN, mémoire non contiguë → repli identique à l'ancien
    code : ce chemin ne peut pas RATER, au pire il ne gagne rien."""
    src = np.ascontiguousarray(src).view(np.uint8).reshape(-1)
    n = min(dst.nbytes, src.nbytes)
    if NT_ENABLED and n >= NT_MIN and dst.flags["C_CONTIGUOUS"]:
        lib = _nt_load()
        if lib is not None:
            gagne = _nt_verdict.get(n)
            if gagne is None:
                gagne = _nt_verdict[n] = _nt_calibrer(lib, n)
            if gagne:
                lib.bobi_copy_nt(dst.ctypes.data, src.ctypes.data, n)
                return n
    dst[:n] = src[:n]
    return n


def _v210_load():
    """Charge libbobi_v210 (variante AVX2 _v3 si le CPU la supporte). None → repli numpy."""
    global _v210_lib
    if _v210_lib is not None:
        return _v210_lib or None
    avx2 = False
    try:
        with open("/proc/cpuinfo") as f:
            avx2 = " avx2 " in f.read().replace("\n", " ")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("BOBI_V210_LIB")]
    for d in ("/usr/local/lib", here):
        if avx2:
            cands.append(os.path.join(d, "libbobi_v210_v3.so"))
        cands.append(os.path.join(d, "libbobi_v210.so"))
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
        except OSError:
            continue
        lib.bobi_v210_stride.restype = ctypes.c_size_t
        lib.bobi_v210_stride.argtypes = [ctypes.c_int]
        for fn, sample_t in (("10", ctypes.POINTER(ctypes.c_uint16)),
                             ("8", _PU8)):
            u = getattr(lib, "bobi_v210_unpack" + fn)
            u.restype = None
            u.argtypes = [_PU8, ctypes.c_size_t, sample_t, sample_t, sample_t,
                          ctypes.c_int, ctypes.c_int]
            p = getattr(lib, "bobi_v210_pack" + fn)
            p.restype = None
            p.argtypes = [sample_t, sample_t, sample_t, _PU8, ctypes.c_size_t,
                          ctypes.c_int, ctypes.c_int]
        _v210_lib = lib
        return lib
    _v210_lib = False
    return None


def _v210_planes(planar, width, height, dtype):
    """Découpe un buffer planar contigu (layout frame_bytes 422) en vues (Y, Cb, Cr)."""
    a = np.ascontiguousarray(planar).view(dtype).reshape(-1)
    ny, nc = width * height, (width // 2) * height
    if a.size < ny + 2 * nc:
        raise ValueError(f"planar trop court : {a.size} samples < {ny + 2 * nc} "
                         f"({width}x{height} 422)")
    return a[:ny], a[ny:ny + nc], a[ny + nc:ny + 2 * nc]


# Ordre des 12 échantillons d'un groupe de 6 px (mots w0..w3, champs bas→haut) :
# [Cb0 Y0 Cr0 | Y1 Cb2 Y2 | Cr2 Y3 Cb4 | Y4 Cr4 Y5] → Y aux indices impairs,
# Cb aux indices 0,4,8 et Cr aux indices 2,6,10 (repli numpy).
def _v210_unpack_np(src, width, height, bit_depth):
    stride = v210_stride(width)
    ngrp = -(-width // 6)                                    # groupes, queue incluse
    w = (np.frombuffer(src, dtype="<u4", count=(stride // 4) * height)
           .reshape(height, stride // 4)[:, :4 * ngrp].reshape(height, ngrp, 4))
    flat = np.empty((height, ngrp, 12), dtype=np.uint16)
    flat[:, :, 0::3] = (w & 0x3FF).astype(np.uint16)
    flat[:, :, 1::3] = ((w >> 10) & 0x3FF).astype(np.uint16)
    flat[:, :, 2::3] = ((w >> 20) & 0x3FF).astype(np.uint16)
    flat = flat.reshape(height, ngrp * 12)
    y = flat[:, 1::2][:, :width]
    cbcr = flat[:, 0::2]                                     # [cb cr cb cr …]
    cb = cbcr[:, 0::2][:, :width // 2]
    cr = cbcr[:, 1::2][:, :width // 2]
    if int(bit_depth) <= 8:
        out = np.empty(frame_bytes(width, height, "422", 8), dtype=np.uint8)
        oy, ocb, ocr = _v210_planes(out, width, height, np.uint8)
        np.right_shift(y, 2, out=oy.reshape(height, width), casting="unsafe")
        np.right_shift(cb, 2, out=ocb.reshape(height, width // 2), casting="unsafe")
        np.right_shift(cr, 2, out=ocr.reshape(height, width // 2), casting="unsafe")
        return out
    out = np.empty(frame_bytes(width, height, "422", 10) // 2, dtype=np.uint16)
    oy, ocb, ocr = _v210_planes(out, width, height, np.uint16)
    oy.reshape(height, width)[:] = y
    ocb.reshape(height, width // 2)[:] = cb
    ocr.reshape(height, width // 2)[:] = cr
    return out


def _v210_pack_np(y, cb, cr, out, width, height, bit_depth):
    stride = v210_stride(width)
    ngrp = -(-width // 6)
    y = y.reshape(height, width).astype(np.uint16)
    cb = cb.reshape(height, width // 2).astype(np.uint16)
    cr = cr.reshape(height, width // 2).astype(np.uint16)
    if int(bit_depth) <= 8:
        y, cb, cr = y << 2, cb << 2, cr << 2
    # Compléter la queue (<6 px) par réplication du dernier échantillon (comme le C).
    def _padded(a, n):
        if a.shape[1] == n:
            return a
        return np.concatenate([a, np.repeat(a[:, -1:], n - a.shape[1], axis=1)], axis=1)
    y = _padded(y, ngrp * 6)
    cb = _padded(cb, ngrp * 3)
    cr = _padded(cr, ngrp * 3)
    flat = np.empty((height, ngrp * 12), dtype=np.uint32)
    flat[:, 1::2] = y
    flat[:, 0::4] = cb
    flat[:, 2::4] = cr
    flat = flat.reshape(height, ngrp, 4, 3)
    words = flat[:, :, :, 0] | (flat[:, :, :, 1] << 10) | (flat[:, :, :, 2] << 20)
    lines = out.reshape(height, stride)
    lines[:, :16 * ngrp] = words.reshape(height, -1).astype("<u4").view(np.uint8)
    lines[:, 16 * ngrp:] = 0                                 # padding d'alignement (spec)
    return out


def v210_unpack(src, width, height, bit_depth=8, out=None):
    """v210 → planar contigu (layout frame_bytes 422). `src` = buffer/ndarray uint8 d'au moins
    v210_frame_bytes(). Renvoie un ndarray uint8 (bit_depth≤8, v>>2) ou uint16 PLANAR10LE.
    `out` optionnel = buffer planar écrivable (ex. la vue grain d'un Writer : zéro-copie)."""
    width, height = int(width), int(height)
    src = np.ascontiguousarray(src).view(np.uint8).reshape(-1)
    need = v210_frame_bytes(width, height)
    if src.size < need:
        raise ValueError(f"buffer v210 trop court : {src.size} < {need}")
    lib = _v210_load()
    if lib is None:
        res = _v210_unpack_np(src[:need].tobytes(), width, height, bit_depth)
        if out is None:
            return res
        o = out.view(np.uint8).reshape(-1)
        r = res.view(np.uint8).reshape(-1)
        o[:r.size] = r
        return out
    if int(bit_depth) <= 8:
        if out is None:
            out = np.empty(frame_bytes(width, height, "422", 8), dtype=np.uint8)
        y, cb, cr = _v210_planes(out, width, height, np.uint8)
        fn, ptr = lib.bobi_v210_unpack8, _PU8
    else:
        if out is None:
            out = np.empty(frame_bytes(width, height, "422", 10) // 2, dtype=np.uint16)
        y, cb, cr = _v210_planes(out, width, height, np.uint16)
        fn, ptr = lib.bobi_v210_unpack10, ctypes.POINTER(ctypes.c_uint16)
    fn(src.ctypes.data_as(_PU8), v210_stride(width),
       y.ctypes.data_as(ptr), cb.ctypes.data_as(ptr), cr.ctypes.data_as(ptr),
       width, height)
    return out


def v210_pack(planar, width, height, bit_depth=8, out=None):
    """Planar contigu (layout frame_bytes 422) → v210. `out` optionnel = vue uint8 écrivable
    (ex. la vue grain d'un Writer v210 : zéro-copie) ; sinon un ndarray neuf est renvoyé."""
    width, height = int(width), int(height)
    dtype = np.uint8 if int(bit_depth) <= 8 else np.uint16
    y, cb, cr = _v210_planes(planar, width, height, dtype)
    need = v210_frame_bytes(width, height)
    if out is None:
        out = np.empty(need, dtype=np.uint8)
    else:
        out = out.view(np.uint8).reshape(-1)
        if out.size < need:
            raise ValueError(f"buffer de sortie v210 trop court : {out.size} < {need}")
        out = out[:need]
    lib = _v210_load()
    if lib is None:
        return _v210_pack_np(y, cb, cr, out, width, height, bit_depth)
    if int(bit_depth) <= 8:
        fn, ptr = lib.bobi_v210_pack8, _PU8
    else:
        fn, ptr = lib.bobi_v210_pack10, ctypes.POINTER(ctypes.c_uint16)
    fn(y.ctypes.data_as(ptr), cb.ctypes.data_as(ptr), cr.ctypes.data_as(ptr),
       out.ctypes.data_as(_PU8), v210_stride(width), width, height)
    return out


# ------------------------------------------------------------- chargement générique des noyaux C
# `charger_noyau(nom, signatures)` charge `libbobi_<nom>.so` en choisissant la variante x86-64-v3
# si le CPU annonce AVX2. C'est le MÉCANISME qui est mutualisé ici — sélection d'ISA, ordre de
# recherche, repli — pas la connaissance des plugins : bobimxl n'a pas à savoir qu'il existe un
# scope. (`_mvk_load` et le v210 sont antérieurs et gardent leur forme nommée ; ils pourront s'y
# ramener, ce n'est pas urgent.)
#
# ★ POURQUOI ICI ET PAS DANS LE PLUGIN. La sélection de variante est la seule chose qu'il ne faut
# surtout pas rater : un `.so` bâti pour une micro-architecture plus riche que le nœud lève SIGILL
# (vécu le 2026-08-22 en portant une image Cascade Lake vers un Broadwell). La dupliquer dans
# chaque plugin, c'est la rater une fois sur trois.
#
# ★ L'APPELANT DOIT SE PROTÉGER. `bobimxl` vit dans l'IMAGE, les plugins sont POUSSÉS : un plugin
# récent peut atterrir sur une image ancienne où cette fonction n'existe pas encore. Appeler
#     getattr(bobimxl, "charger_noyau", lambda *_a, **_k: None)(...)
# transforme un plantage en dégradation visible — c'est ce que fait déjà le multiview avec
# `mvk_available`, et c'est le bon réflexe tant qu'aucune exigence de version d'image n'est
# vérifiée au déploiement.

_noyaux = {}                # nom → CDLL chargée, ou False si introuvable


def charger_noyau(nom, signatures):
    """Charge `libbobi_<nom>.so` et déclare les signatures ctypes de ses fonctions.

    `signatures` : {"nom_de_fonction": [type_ctypes, ...]}. Le type de retour est toujours None —
    nos noyaux écrivent dans des tampons fournis par l'appelant, ils ne renvoient rien.
    Variable d'environnement `BOBI_<NOM>_LIB` pour forcer un chemin (banc).
    Rend la CDLL, ou None si introuvable ou si une signature manque."""
    if nom in _noyaux:
        return _noyaux[nom] or None
    avx2 = False
    try:
        with open("/proc/cpuinfo") as f:
            avx2 = " avx2 " in f.read().replace("\n", " ")
    except OSError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("BOBI_%s_LIB" % nom.upper())]
    for d in ("/usr/local/lib", here):
        if avx2:
            cands.append(os.path.join(d, "libbobi_%s_v3.so" % nom))
        cands.append(os.path.join(d, "libbobi_%s.so" % nom))
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
        except OSError:
            continue
        try:
            for fn, args in signatures.items():
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = list(args)
        except AttributeError:
            # Le .so existe mais ne porte pas ce qu'on attend : une variante périmée traîne dans
            # l'image. Mieux vaut le repli numpy qu'un appel sur une signature fausse.
            continue
        _noyaux[nom] = lib
        return lib
    _noyaux[nom] = False
    return None


# ------------------------------------------------------------------- mvk (compose fusionné CPU)
# Passes de compositing multiview fusionnées en C (libbobi_mvk.so, script_templates/mvcompose.c) :
# blend / blend_pre / place nearest en UNE passe mémoire chacune, au lieu des passes numpy
# chaînées (memory-bound — banc 2026-07-11 : 7-40× selon la profondeur du pipeline). Contrat :
# les wrappers *_into renvoient False si la lib est absente ou si les tableaux ne conviennent
# pas (dtype/forme/contiguïté du dernier axe) → l'appelant DOIT garder son repli numpy, qui
# reste la référence bit-exacte. Les INDEX nearest sont calculés par l'appelant (les deux
# formules du multiview — troncature float de resize_plane, division entière du chemin
# tranche — donnent des octets différents ; le C n'en choisit aucune).

_mvk_lib = None             # CDLL chargée, ou False si introuvable (repli numpy)


def _mvk_load():
    """Charge libbobi_mvk (variante AVX2 _v3 si le CPU la supporte) et pose le nb de threads
    OpenMP : env BOBI_MVK_THREADS, sinon cœurs PHYSIQUES du cpuset (HT-aware : le cpuset posé
    par core_pool contient les 2 logiques de chaque cœur → //2). None → repli numpy."""
    global _mvk_lib
    if _mvk_lib is not None:
        return _mvk_lib or None
    avx2 = False
    try:
        with open("/proc/cpuinfo") as f:
            avx2 = " avx2 " in f.read().replace("\n", " ")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("BOBI_MVK_LIB")]
    for d in ("/usr/local/lib", here):
        if avx2:
            cands.append(os.path.join(d, "libbobi_mvk_v3.so"))
        cands.append(os.path.join(d, "libbobi_mvk.so"))
    # AVANT le chargement (libgomp lit l'env à SON init) : attente PASSIVE des threads OpenMP.
    # Le défaut libgomp (spin-wait ~300 ms après chaque région parallèle) laissait les threads
    # tourner À VIDE entre deux appels mvk : sur un cpuset de N CPU avec N threads, ils
    # affamaient tout le reste du process (PIL, lectures, sortie) — mesuré sur le vmid 145 :
    # ov_render 6,8 → 29,7 ms, fps 50 → 15. Passif = le fork/join coûte quelques µs de plus,
    # négligeable devant les régions (~ms) ; l'utilisateur peut surcharger via l'env.
    os.environ.setdefault("OMP_WAIT_POLICY", "passive")
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
        except OSError:
            continue
        _pd, _i64, _i32 = ctypes.c_ssize_t, ctypes.c_int64, ctypes.c_int
        for fn in ("mvk_blend_u8", "mvk_blend_u16"):
            f = getattr(lib, fn)
            f.restype = None
            f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                          ctypes.c_void_p, _pd, _i32, _i32]
        for fn in ("mvk_blend_pre_u8", "mvk_blend_pre_u16"):
            f = getattr(lib, fn)
            f.restype = None
            f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                          ctypes.c_void_p, _pd, _i32, _i32]
        for fn in ("mvk_place_u8", "mvk_place_u16"):
            f = getattr(lib, fn)
            f.restype = None
            f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                          ctypes.c_void_p, _i64, _i64, ctypes.c_void_p, _i32, _i32]
        lib.mvk_set_threads.restype = None
        lib.mvk_set_threads.argtypes = [_i32]
        lib.mvk_get_threads.restype = _i32
        lib.mvk_get_threads.argtypes = []
        # ABI 2 (image ≥ 0.12) : conversion RGBA→YUV fusionnée. Binding TOLÉRANT — un .so
        # d'image 0.11 (ABI 1) n'a pas ces symboles : les blends/place restent servis, et
        # mvk_rgba2yuv() renvoie None (repli numpy à l'appelant). bobimxl étant poussé au
        # déploiement, il croise couramment des .so plus vieux que lui.
        global _MVK_HAS_R2Y, _MVK_HAS_MIX
        try:
            for fn in ("mvk_rgba2yuv_u8_u8", "mvk_rgba2yuv_u8_u16",
                       "mvk_rgba2yuv_f32_u8", "mvk_rgba2yuv_f32_u16"):
                f = getattr(lib, fn)
                f.restype = _i32
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              _i32, _i32, _i32, _i32, _i32, _i32]
            _MVK_HAS_R2Y = True
        except AttributeError:
            _MVK_HAS_R2Y = False
        # ABI 3 (image ≥ 0.13) : mix pondéré float32 A/B (transitions du mixer). Tolérant.
        try:
            for fn in ("mvk_mixf_u8", "mvk_mixf_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, _i32, _i32,
                              ctypes.c_double, ctypes.c_double, _i32, _i32]
            for fn in ("mvk_mixmap_u8", "mvk_mixmap_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, _pd, _i64,
                              _i32, _i32]
            _MVK_HAS_MIX = True
        except AttributeError:
            _MVK_HAS_MIX = False
        # ABI 4 (image ≥ 0.15) : compositing du plugin SPLIT (gather ⊗ ruban ⊗ blend 256e).
        # Tolérant : un .so plus vieux garde tout le reste, mvk_spl_*_into renvoie False.
        global _MVK_HAS_SPL
        try:
            for fn in ("mvk_spl_compose_u8", "mvk_spl_compose_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, _i32, _i32, _i32]
            for fn in ("mvk_spl_fused_u8", "mvk_spl_fused_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, _i32, _i32, _i32]
            for fn in ("mvk_spl_solid_u8", "mvk_spl_solid_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd, _i32, _i32, _i32]
            _MVK_HAS_SPL = True
        except AttributeError:
            _MVK_HAS_SPL = False
        # ABI 5 (image ≥ 0.16) : fond DÉGRADÉ N arrêts du split (wipe/mélange animable). Tolérant.
        global _MVK_HAS_GRAD
        _f32 = ctypes.c_float
        try:
            for fn in ("mvk_spl_gradient_u8", "mvk_spl_gradient_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              _i32, _i32, _i32, _i32, _i32, _i32,
                              _f32, _f32, _f32, _f32,
                              _i32, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p,
                              _f32, _i32, _f32, ctypes.c_void_p]
            _MVK_HAS_GRAD = True
        except AttributeError:
            _MVK_HAS_GRAD = False
        # ABI 6 (image ≥ 0.22) : CONSTRUCTION d'empreinte du split (régime ANIMÉ). Tolérant.
        global _MVK_HAS_STAMP
        try:
            f = lib.mvk_spl_blur2_f32
            f.restype = None
            f.argtypes = [ctypes.c_void_p, _i32, _i32, _i32, _i32, ctypes.c_void_p]
            f = lib.mvk_spl_rotmap
            f.restype = None
            f.argtypes = ([_i32, _i32] + [ctypes.c_void_p] * 4 +
                          [_f32, _f32, _f32, _f32, _i32, _i32,
                           _f32, _f32, _f32, _f32,
                           _i32, _i32, _i32, _i32, _i32, _i32,
                           _i32, _f32, _i32, _f32,
                           _f32, _f32, _f32, _f32,
                           _i32, _f32] + [ctypes.c_void_p] * 6)
            for fn in ("mvk_spl_rotring_u8", "mvk_spl_rotring_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = ([_i32, _i32] + [ctypes.c_void_p] * 4 +
                              [_f32, _f32, _f32, _f32,
                               _f32, _i32, _f32, _f32,
                               _f32, _f32, _f32, _f32, _f32,
                               _f32, _f32, _f32] + [ctypes.c_void_p] * 2)
            _MVK_HAS_STAMP = True
        except AttributeError:
            _MVK_HAS_STAMP = False
        # ABI 7 (image ≥ 0.23) : pré-fusion ruban⊗alpha + produit externe (chemin NON TOURNÉ).
        global _MVK_HAS_FUSE
        try:
            for fn in ("mvk_spl_prefuse_u8", "mvk_spl_prefuse_u16"):
                f = getattr(lib, fn)
                f.restype = None
                f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              ctypes.c_void_p, _pd, ctypes.c_void_p, _pd, ctypes.c_void_p, _pd,
                              _i32, _i32]
            f = lib.mvk_spl_outer_u16
            f.restype = None
            f.argtypes = [ctypes.c_void_p, _pd, ctypes.c_void_p, ctypes.c_void_p, _i32, _i32]
            _MVK_HAS_FUSE = True
        except AttributeError:
            _MVK_HAS_FUSE = False
        try:
            n = int(os.environ.get("BOBI_MVK_THREADS") or 0)
        except ValueError:
            n = 0
        if n < 1:
            n = _mvk_default_threads()
        lib.mvk_set_threads(n)
        _mvk_lib = lib
        return lib
    _mvk_lib = False
    return None


def _mvk_default_threads():
    """Nb de threads OpenMP par défaut = CŒURS PHYSIQUES réels de l'affinité, comptés via la
    topologie sysfs (package_id + core_id — core_id seul se répète entre sockets). Un cpuset
    core_pool peut contenir des paires HT (« 19-23,43-47 » → 5 cœurs / 10 logiques) COMME des
    logiques sans sibling (« 19-21 » → 3 cœurs / 3 logiques) : l'ancien « affinité // 2 »
    sous-comptait le second cas (1 thread au lieu de 3, gain MT du place perdu — vu sur le
    vmid 145). Repli « // 2 » si la topologie est illisible (conteneur sans /sys).
    PLAFOND à 4 threads : un conteneur SANS cpuset (ex. assembleur du tissu) voit TOUTE la
    machine (48 logiques sur dl360) — sans plafond il lançait 24 threads qui piétinaient
    les cœurs du moteur DPDK (fab 148 mesuré à 14 fps). 4 = le genou du banc (op memory-
    bound : 4,8× à 4 thr, plus au-delà ≈ rien). BOBI_MVK_THREADS pour dépasser le plafond."""
    try:
        aff = os.sched_getaffinity(0)
    except Exception:
        return 1
    cores = set()
    for c in aff:
        base = "/sys/devices/system/cpu/cpu%d/topology/" % c
        try:
            with open(base + "physical_package_id") as f:
                pkg = f.read().strip()
            with open(base + "core_id") as f:
                cores.add((pkg, f.read().strip()))
        except Exception:
            return max(1, min(4, len(aff) // 2))
    return max(1, min(4, len(cores)))


def mvk_threads():
    """Nb de threads OpenMP effectif du kernel (0 si lib absente) — exposé aux métriques."""
    lib = _mvk_load()
    return int(lib.mvk_get_threads()) if lib is not None else 0


def mvk_available():
    """True si le kernel C fusionné est chargeable (à exposer dans les métriques :8080)."""
    return _mvk_load() is not None


def mvk_set_threads(n):
    """Force le nb de threads OpenMP du kernel (déterministe : n ne change pas les octets)."""
    lib = _mvk_load()
    if lib is not None:
        lib.mvk_set_threads(int(n))


def _mvk_ok2d(a, dtypes):
    """Tableau utilisable par le kernel : 2D, dtype attendu, dernier axe contigu."""
    return (a is not None and getattr(a, "ndim", 0) == 2 and a.dtype in dtypes
            and a.strides[1] == a.itemsize)


def mvk_blend_into(dst, src, alpha):
    """dst ← (dst·(255−α) + src·α) // 255, IN-PLACE, bit-exact au blend numpy du multiview.
    dst/src uint8 ou uint16 (10/12 bits), alpha uint8, mêmes formes ; dst peut être une vue
    stridée (bbox d'un canvas). Renvoie False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None:
        return False
    dt = (np.uint8,) if dst.dtype == np.uint8 else (np.uint16,)
    if not (_mvk_ok2d(dst, dt) and _mvk_ok2d(src, dt) and _mvk_ok2d(alpha, (np.uint8,))):
        return False
    if not (dst.shape == src.shape == alpha.shape):
        return False
    h, w = dst.shape
    fn = lib.mvk_blend_u8 if dst.dtype == np.uint8 else lib.mvk_blend_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       src.ctypes.data, src.strides[0] // src.itemsize,
       alpha.ctypes.data, alpha.strides[0] // alpha.itemsize, h, w)
    return True


def mvk_blend_pre_into(dst, inv_a, src_a):
    """dst ← (dst·inv_a + src_a) // 255, IN-PLACE (opérandes pré-calculés du chrome multiview :
    inv_a = 255−α, src_a = src·α, dtype _ACC = uint16 si dst uint8, uint32 si dst uint16).
    Renvoie False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None:
        return False
    if dst.dtype == np.uint8:
        fn, acc = lib.mvk_blend_pre_u8, (np.uint16,)
        if not _mvk_ok2d(dst, (np.uint8,)):
            return False
    elif dst.dtype == np.uint16:
        fn, acc = lib.mvk_blend_pre_u16, (np.uint32,)
        if not _mvk_ok2d(dst, (np.uint16,)):
            return False
    else:
        return False
    if not (_mvk_ok2d(inv_a, acc) and _mvk_ok2d(src_a, acc)):
        return False
    if not (dst.shape == inv_a.shape == src_a.shape):
        return False
    h, w = dst.shape
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       inv_a.ctypes.data, inv_a.strides[0] // inv_a.itemsize,
       src_a.ctypes.data, src_a.strides[0] // src_a.itemsize, h, w)
    return True


_MVK_HAS_R2Y = False
_MVK_HAS_MIX = False


def _mvk_mix_ok(a, dt):
    return (getattr(a, "ndim", 0) == 2 and a.dtype == dt and a.strides[1] == a.itemsize)


def mvk_mixf_into(dst, a, b, coef_a, coef_b, clip=False, maxv=255):
    """dst ← trunc(a·coef_a + b·coef_b) en float32, clamp [0, maxv] si clip — bit-exact aux
    transitions du mixer (dissolve : coef_a=1−α/coef_b=α sans clip ; additif luma : clip).
    dst/a/b 2D même forme, dernier axe contigu, uint8 ou uint16 ; dst PEUT aliaser a (keyer).
    False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_MIX:
        return False
    dt = dst.dtype
    if dt not in (np.uint8, np.uint16):
        return False
    if not (_mvk_mix_ok(dst, dt) and _mvk_mix_ok(a, dt) and _mvk_mix_ok(b, dt)):
        return False
    if not (dst.shape == a.shape == b.shape):
        return False
    h, w = dst.shape
    fn = lib.mvk_mixf_u8 if dt == np.uint8 else lib.mvk_mixf_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       a.ctypes.data, a.strides[0] // a.itemsize,
       b.ctypes.data, b.strides[0] // b.itemsize,
       h, w, float(coef_a), float(coef_b), 1 if clip else 0, int(maxv))
    return True


def mvk_mixmap_into(dst, a, b, mask, m_colstep=1):
    """dst ← trunc(a·(1−m) + b·m), m = masque float32 (wipe du mixer). `mask` = vue 2D du
    masque (ligne-stridée OK) ; m_colstep = pas de colonne SUPPLÉMENTAIRE échantillonné dans
    le masque (chroma : passer le masque plein + m_colstep=_CW). Pas de clip (comme numpy).
    False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_MIX:
        return False
    dt = dst.dtype
    if dt not in (np.uint8, np.uint16):
        return False
    if not (_mvk_mix_ok(dst, dt) and _mvk_mix_ok(a, dt) and _mvk_mix_ok(b, dt)):
        return False
    if not (dst.shape == a.shape == b.shape):
        return False
    if mask.dtype != np.float32 or mask.ndim != 2:
        return False
    h, w = dst.shape
    if mask.strides[1] % mask.itemsize:
        return False
    m_colstep = int(m_colstep) * (mask.strides[1] // mask.itemsize)
    fn = lib.mvk_mixmap_u8 if dt == np.uint8 else lib.mvk_mixmap_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       a.ctypes.data, a.strides[0] // a.itemsize,
       b.ctypes.data, b.strides[0] // b.itemsize,
       mask.ctypes.data, mask.strides[0] // mask.itemsize, m_colstep, h, w)
    return True


def mvk_rgba2yuv(arr, out_dtype, scale, maxv, cw, ch):
    """RGBA interleavé (H, W, 4) contigu, uint8 (chemin PIL) ou float32 (chemin tuile xp) →
    plans (y, u, v) en out_dtype (uint8/uint16), chroma sous-échantillonnée cw×ch, EN UNE
    passe C — bit-exact aux expressions float32 numpy de rgba_to_yuv/_rgba_to_yuv_xp (le .so
    est compilé -ffp-contract=off). L'ALPHA reste à l'appelant. Renvoie None si lib absente,
    ABI 1 (image 0.11) ou entrée non conforme → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_R2Y:
        return None
    if getattr(arr, "ndim", 0) != 3 or arr.shape[2] != 4 or not arr.flags.c_contiguous:
        return None
    h, w = arr.shape[0], arr.shape[1]
    if w > 8192 or w % cw or h % ch or h == 0 or w == 0:
        return None
    if arr.dtype == np.uint8:
        pfx = "u8"
    elif arr.dtype == np.float32:
        pfx = "f32"
    else:
        return None
    out_dtype = np.dtype(out_dtype)
    if out_dtype == np.uint8:
        fn = getattr(lib, "mvk_rgba2yuv_%s_u8" % pfx)
    elif out_dtype == np.uint16:
        fn = getattr(lib, "mvk_rgba2yuv_%s_u16" % pfx)
    else:
        return None
    y = np.empty((h, w), dtype=out_dtype)
    u = np.empty((h // ch, w // cw), dtype=out_dtype)
    v = np.empty((h // ch, w // cw), dtype=out_dtype)
    rc = fn(arr.ctypes.data, w, y.ctypes.data, w,
            u.ctypes.data, w // cw, v.ctypes.data, w // cw,
            h, w, int(cw), int(ch), int(scale), int(maxv))
    if rc != 0:
        return None
    return y, u, v


def mvk_place_into(dst, src, row_idx, col_idx=None, col0=0, col_step=0):
    """dst[r, c] ← src[row_idx[r], col0 + c·col_step] (col_step > 0, décimation régulière)
    ou src[row_idx[r], col_idx[c]] (gather générique). row_idx/col_idx = indices SOURCE
    absolus int32 calculés par l'appelant (bit-exact à sa propre formule nearest). src doit
    être ligne-contigu (plan/vue dont le dernier axe est contigu) ; dst = vue bbox du canvas.
    Renvoie False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None:
        return False
    dt = (np.uint8,) if dst.dtype == np.uint8 else (np.uint16,)
    if not (_mvk_ok2d(dst, dt) and _mvk_ok2d(src, dt)):
        return False
    h, w = dst.shape
    if row_idx.dtype != np.int32 or not row_idx.flags.c_contiguous or row_idx.size != h:
        return False
    if col_step <= 0:
        if col_idx is None or col_idx.dtype != np.int32 \
                or not col_idx.flags.c_contiguous or col_idx.size != w:
            return False
        cptr = col_idx.ctypes.data
    else:
        cptr = None
    fn = lib.mvk_place_u8 if dst.dtype == np.uint8 else lib.mvk_place_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       src.ctypes.data, src.strides[0] // src.itemsize,
       row_idx.ctypes.data, int(col0), int(col_step), cptr, h, w)
    return True


# ---------------------------------------------------------- mvk spl_* : compositing du SPLIT
# ABI 4 (image ≥ 0.15). Arithmétique en 256e (alpha 0..256, >> 8) — celle du plugin split, ≠
# des blends multiview (255e) : ne PAS mélanger les deux familles. Accumulation uint32 côté C.
# Les cartes d'index (flat 2D int64 pour une box TOURNÉE, row/col int32 pour une box droite)
# sont calculées par l'appelant → bit-exact à sa propre formule nearest. Wrappers défensifs :
# False si la lib est absente, l'ABI trop vieille ou les tableaux non conformes (dtype/forme/
# contiguïté du dernier axe) → l'appelant DOIT garder son repli numpy.

_MVK_HAS_SPL = False


def _spl_idx(flat, row_idx, col_idx, h, w):
    """Valide les cartes d'index et renvoie (flat_ptr, flat_stride, row_ptr, col_ptr) ou None."""
    if flat is not None:
        if (flat.ndim != 2 or flat.dtype != np.int64 or flat.shape != (h, w)
                or flat.strides[1] != flat.itemsize):
            return None
        return (flat.ctypes.data, flat.strides[0] // flat.itemsize, None, None)
    if row_idx is None or col_idx is None:
        return None
    if (row_idx.dtype != np.int32 or col_idx.dtype != np.int32
            or not row_idx.flags.c_contiguous or not col_idx.flags.c_contiguous
            or row_idx.size != h or col_idx.size != w):
        return None
    return (None, 0, row_idx.ctypes.data, col_idx.ctypes.data)


def _spl_map(a, dt, h, w):
    """Carte 2D (masque/ruban) du bon dtype, forme (h, w), dernier axe contigu — ou None."""
    if a is None:
        return None
    if a.ndim != 2 or a.dtype != dt or a.shape != (h, w) or a.strides[1] != a.itemsize:
        return False          # non conforme (≠ absent) → repli
    return a


def mvk_spl_compose_into(dst, src, flat=None, row_idx=None, col_idx=None,
                         ring_a=None, ring_c=None, alpha=None, a_scalar=256):
    """SPLIT — gather + ruban de bordure + blend alpha, EN UNE PASSE :
        k   = src[flat[r,c]]  ou  src[row_idx[r], col_idx[c]]
        k   = (k·(256−ring_a) + ring_c·ring_a) >> 8        (si ruban)
        dst = (dst·(256−a) + k·a) >> 8                     (a = alpha[r,c] sinon a_scalar ;
                                                            a ≥ 256 → copie directe)
    dst = vue 2D du canvas (uint8/uint16), src = plan source ligne-contigu.
    False si non applicable → repli numpy à l'appelant."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_SPL:
        return False
    dt = dst.dtype
    if dt not in (np.uint8, np.uint16) or dst.ndim != 2 or dst.strides[1] != dst.itemsize:
        return False
    if src.dtype != dt or src.ndim != 2 or src.strides[1] != src.itemsize:
        return False
    h, w = dst.shape
    idx = _spl_idx(flat, row_idx, col_idx, h, w)
    if idx is None:
        return False
    fptr, fstr, rptr, cptr = idx
    ra = _spl_map(ring_a, np.uint16, h, w)
    rc = _spl_map(ring_c, dt, h, w)
    al = _spl_map(alpha, np.uint16, h, w)
    if ra is False or rc is False or al is False:
        return False
    if (ra is None) != (rc is None):        # ruban = couple (masque, couleur)
        return False
    fn = lib.mvk_spl_compose_u8 if dt == np.uint8 else lib.mvk_spl_compose_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       src.ctypes.data, src.strides[0] // src.itemsize,
       fptr, fstr, rptr, cptr,
       (ra.ctypes.data if ra is not None else None),
       (ra.strides[0] // ra.itemsize if ra is not None else 0),
       (rc.ctypes.data if rc is not None else None),
       (rc.strides[0] // rc.itemsize if rc is not None else 0),
       (al.ctypes.data if al is not None else None),
       (al.strides[0] // al.itemsize if al is not None else 0),
       int(a_scalar), h, w)
    return True


def mvk_spl_fused_into(dst, src, inv_a, a1, c2, flat=None, row_idx=None, col_idx=None, maxv=255):
    """SPLIT — chemin PRÉ-FUSIONNÉ du stamp (bordure ⊗ alpha précalculés, cf. _build_stamp) :
        dst = clip((dst·inv_a >> 8) + (src[idx]·A1 >> 8) + C2, 0, maxv)
    inv_a/A1 uint16, C2 uint32, mêmes formes que dst. False → repli numpy."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_SPL:
        return False
    dt = dst.dtype
    if dt not in (np.uint8, np.uint16) or dst.ndim != 2 or dst.strides[1] != dst.itemsize:
        return False
    if src.dtype != dt or src.ndim != 2 or src.strides[1] != src.itemsize:
        return False
    h, w = dst.shape
    idx = _spl_idx(flat, row_idx, col_idx, h, w)
    if idx is None:
        return False
    fptr, fstr, rptr, cptr = idx
    ia = _spl_map(inv_a, np.uint16, h, w)
    a1m = _spl_map(a1, np.uint16, h, w)
    c2m = _spl_map(c2, np.uint32, h, w)
    if not (isinstance(ia, np.ndarray) and isinstance(a1m, np.ndarray)
            and isinstance(c2m, np.ndarray)):
        return False
    fn = lib.mvk_spl_fused_u8 if dt == np.uint8 else lib.mvk_spl_fused_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       src.ctypes.data, src.strides[0] // src.itemsize,
       fptr, fstr, rptr, cptr,
       ia.ctypes.data, ia.strides[0] // ia.itemsize,
       a1m.ctypes.data, a1m.strides[0] // a1m.itemsize,
       c2m.ctypes.data, c2m.strides[0] // c2m.itemsize,
       h, w, int(maxv))
    return True


def mvk_spl_solid_into(dst, alpha, val):
    """SPLIT — blend d'une couleur UNIE sous masque (ombre portée) : dst = (dst·(256−a) + val·a) >> 8.
    False → repli numpy."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_SPL:
        return False
    dt = dst.dtype
    if dt not in (np.uint8, np.uint16) or dst.ndim != 2 or dst.strides[1] != dst.itemsize:
        return False
    h, w = dst.shape
    al = _spl_map(alpha, np.uint16, h, w)
    if not isinstance(al, np.ndarray):
        return False
    fn = lib.mvk_spl_solid_u8 if dt == np.uint8 else lib.mvk_spl_solid_u16
    fn(dst.ctypes.data, dst.strides[0] // dst.itemsize,
       al.ctypes.data, al.strides[0] // al.itemsize, int(val), h, w)
    return True


_MVK_HAS_FUSE = False
_MVK_HAS_STAMP = False
_MVK_HAS_GRAD = False


def mvk_spl_gradient_into(y, u, v, W, H, cw, ch, y0, y1, cosv, sinv, pmin, inv_range,
                          pos, yv, uv, vv, softness, maxv, amp, bayer):
    """SPLIT — remplit la BANDE [y0,y1) des plans Y/U/V avec un DÉGRADÉ N arrêts (wipe/mélange),
    tramé (Bayer 8×8). y/u/v = plans 2D contigus (dernier axe) ; pos/yv/uv/vv/bayer = float32
    contigus (arrêts YUV DÉJÀ à la profondeur, table Bayer 64 floats). False → repli numpy."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_GRAD:
        return False
    dt = y.dtype
    if dt not in (np.uint8, np.uint16):
        return False
    for a in (y, u, v):
        if a.dtype != dt or a.ndim != 2 or a.strides[1] != a.itemsize:
            return False
    for a in (pos, yv, uv, vv, bayer):
        if a.dtype != np.float32 or not a.flags.c_contiguous:
            return False
    if not (pos.size == yv.size == uv.size == vv.size) or pos.size < 2 or bayer.size != 64:
        return False
    fn = lib.mvk_spl_gradient_u8 if dt == np.uint8 else lib.mvk_spl_gradient_u16
    fn(y.ctypes.data, y.strides[0] // y.itemsize,
       u.ctypes.data, u.strides[0] // u.itemsize,
       v.ctypes.data, v.strides[0] // v.itemsize,
       int(W), int(H), int(cw), int(ch), int(y0), int(y1),
       float(cosv), float(sinv), float(pmin), float(inv_range),
       int(pos.size), pos.ctypes.data, yv.ctypes.data, uv.ctypes.data, vv.ctypes.data,
       float(softness), int(maxv), float(amp), bayer.ctypes.data)
    return True


# ── ABI 6 : CONSTRUCTION d'empreinte du plugin SPLIT (régime ANIMÉ) ────────────────────────────
# Ces trois wrappers ne composent rien : ils bâtissent l'empreinte d'une box. Ils n'ont d'effet
# que quand la géométrie CHANGE à chaque trame (transition, curseur glissé, rotation animée) —
# le cas où le cache de stamp ne sert à rien et où la construction numpy domine la trame.
# Chaque kernel est BIT-EXACT avec son repli numpy (cf. mvcompose.c). False → repli.

def mvk_spl_blur2(m, r, iters=3):
    """SPLIT — flou ~gaussien 2D EN PLACE d'un masque non séparable (silhouette d'une box
    tournée) : `iters` box-blurs séparés en 2 passes 1D. `m` = float32 2D C-contigu, modifié
    sur place. Remplace 6 × (pad + cumsum + slice + divide) numpy par 2 passes. False → repli."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_STAMP:
        return False
    if m.dtype != np.float32 or m.ndim != 2 or not m.flags.c_contiguous:
        return False
    h, w = m.shape
    if h < 1 or w < 1:
        return False
    scratch = np.empty((h, w), dtype=np.float32)
    lib.mvk_spl_blur2_f32(m.ctypes.data, int(h), int(w), int(r), int(iters), scratch.ctypes.data)
    return True


def mvk_spl_rotmap(aw, ah, ys, yc, xc, nxs, hw, hh, fdw, fdh, mh, mv,
                   sxr0, dsx, syr0, dsy, W, H, UVW, UVH, cw, ch,
                   use_fe, fe, rad, csoft, cadd, cref_x, cref_y, apply_op, op,
                   want_dedge=True, want_rowmax=False):
    """SPLIT — empreinte d'une box TOURNÉE en UNE passe sur l'AABB : carte d'échantillonnage
    (index plat luma + chroma), masque alpha (feather × coins × opacité), distance signée aux
    bords (réutilisée par l'ombre et le ruban) et max d'index source par ligne (mode tranche).
    Renvoie (flat, flat2, a16, a16_2, dedge, rowmax) ou None → repli numpy."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_STAMP:
        return None
    aw = int(aw); ah = int(ah)
    if aw < 1 or ah < 1:
        return None
    for a in (ys, yc, xc, nxs):
        if a.dtype != np.float32 or not a.flags.c_contiguous:
            return None
    if ys.size != ah or yc.size != ah or xc.size != aw or nxs.size != aw:
        return None
    aw2 = (aw + int(cw) - 1) // int(cw)
    ah2 = (ah + int(ch) - 1) // int(ch)
    flat = np.empty(ah * aw, dtype=np.int64)
    flat2 = np.empty(ah2 * aw2, dtype=np.int64)
    a16 = np.empty((ah, aw), dtype=np.uint16)
    a16_2 = np.empty((ah2, aw2), dtype=np.uint16)
    dedge = np.empty((ah, aw), dtype=np.float32) if want_dedge else None
    rowmax = np.empty(ah, dtype=np.int32) if want_rowmax else None
    lib.mvk_spl_rotmap(aw, ah, ys.ctypes.data, yc.ctypes.data, xc.ctypes.data, nxs.ctypes.data,
                       float(hw), float(hh), float(fdw), float(fdh), int(bool(mh)), int(bool(mv)),
                       float(sxr0), float(dsx), float(syr0), float(dsy),
                       int(W), int(H), int(UVW), int(UVH), int(cw), int(ch),
                       int(bool(use_fe)), float(fe), int(rad), float(rad),
                       float(cref_x), float(cref_y), float(csoft), float(cadd),
                       int(bool(apply_op)), float(op),
                       flat.ctypes.data, flat2.ctypes.data, a16.ctypes.data, a16_2.ctypes.data,
                       (dedge.ctypes.data if dedge is not None else None),
                       (rowmax.ctypes.data if rowmax is not None else None))
    return flat, flat2, a16, a16_2, dedge, rowmax


def mvk_spl_rotring(ring_a, ring_y, ys, yc, xc, nxs, hw, hh, sv, cv,
                    inner_w, has_bevel, bevel_abs, sgn, pos_iw, soft_div,
                    lx, ly, lz, Yb, minv, maxv):
    """SPLIT — ruban de bordure 3D d'une box TOURNÉE (bords obliques + normale du biseau tournée
    avec la box) en UNE passe. `ring_a` uint16 2D, `ring_y` uint8/uint16 2D, tous deux
    C-contigus et de même forme. False → repli numpy."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_STAMP:
        return False
    if ring_a.dtype != np.uint16 or not ring_a.flags.c_contiguous:
        return False
    if ring_y.dtype not in (np.uint8, np.uint16) or not ring_y.flags.c_contiguous:
        return False
    if ring_a.shape != ring_y.shape or ring_a.ndim != 2:
        return False
    ah, aw = ring_a.shape
    for a, n in ((ys, ah), (yc, ah), (xc, aw), (nxs, aw)):
        if a.dtype != np.float32 or not a.flags.c_contiguous or a.size != n:
            return False
    fn = lib.mvk_spl_rotring_u8 if ring_y.dtype == np.uint8 else lib.mvk_spl_rotring_u16
    fn(int(aw), int(ah), ys.ctypes.data, yc.ctypes.data, xc.ctypes.data, nxs.ctypes.data,
       float(hw), float(hh), float(sv), float(cv),
       float(inner_w), int(bool(has_bevel)), float(bevel_abs), float(sgn),
       float(pos_iw), float(soft_div), float(lx), float(ly), float(lz),
       float(Yb), float(minv), float(maxv),
       ring_a.ctypes.data, ring_y.ctypes.data)
    return True


def mvk_spl_prefuse(a, ring_a, ring_c, inv_a, A1, C2):
    """SPLIT — pré-fusion ruban ⊗ alpha en UNE passe : inv_a = 256−a, A1 = (a·(256−ring))>>8,
    C2 = (couleur·((a·ring)>>8))>>8. `inv_a`/`A1` peuvent être None (déjà produits par un appel
    précédent : U et V partagent leur alpha et ne diffèrent que par la couleur du ruban).
    Arithmétique ENTIÈRE identique au repli numpy → bit-exact. False → repli."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_FUSE:
        return False
    dt = ring_c.dtype
    if dt not in (np.uint8, np.uint16):
        return False
    for arr, want in ((a, np.uint16), (ring_a, np.uint16), (C2, np.uint32)):
        if arr.dtype != want or arr.ndim != 2 or arr.strides[1] != arr.itemsize:
            return False
    if ring_c.ndim != 2 or ring_c.strides[1] != ring_c.itemsize:
        return False
    h, w = a.shape
    if ring_a.shape != (h, w) or ring_c.shape != (h, w) or C2.shape != (h, w):
        return False
    for arr in (inv_a, A1):
        if arr is not None and (arr.dtype != np.uint16 or arr.shape != (h, w)
                                or arr.strides[1] != arr.itemsize):
            return False
    fn = lib.mvk_spl_prefuse_u8 if dt == np.uint8 else lib.mvk_spl_prefuse_u16
    fn(a.ctypes.data, a.strides[0] // a.itemsize,
       ring_a.ctypes.data, ring_a.strides[0] // ring_a.itemsize,
       ring_c.ctypes.data, ring_c.strides[0] // ring_c.itemsize,
       (inv_a.ctypes.data if inv_a is not None else None),
       (inv_a.strides[0] // inv_a.itemsize if inv_a is not None else 0),
       (A1.ctypes.data if A1 is not None else None),
       (A1.strides[0] // A1.itemsize if A1 is not None else 0),
       C2.ctypes.data, C2.strides[0] // C2.itemsize, int(h), int(w))
    return True


def mvk_spl_outer_u16(out, vy, vx):
    """SPLIT — out[j][i] = (uint16)(vy[j]·vx[i]) en une passe (masque d'ombre d'une box DROITE :
    séparable, mais sa matérialisation reste un plein rect que numpy produit en deux passes)."""
    lib = _mvk_load()
    if lib is None or not _MVK_HAS_FUSE:
        return False
    if out.dtype != np.uint16 or out.ndim != 2 or out.strides[1] != out.itemsize:
        return False
    for v in (vy, vx):
        if v.dtype != np.float32 or v.ndim != 1 or not v.flags.c_contiguous:
            return False
    h, w = out.shape
    if vy.size != h or vx.size != w:
        return False
    lib.mvk_spl_outer_u16(out.ctypes.data, out.strides[0] // out.itemsize,
                          vy.ctypes.data, vx.ctypes.data, int(h), int(w))
    return True
