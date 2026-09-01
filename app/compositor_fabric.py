"""Tissu de composition adressé par contenu — PHASE 1 (pure, hors-ligne, sans déploiement).

Compile les layouts déclaratifs des multiviews (`deploy_config.params`) en un DAG de nœuds de
rendu **adressés par signature de contenu** :

    sources → scale (= pyramide) → cells → regions → outputs

Deux nœuds de **signature identique** produisent exactement les mêmes pixels à chaque instant
(cf. invariant ci-dessous) → ils peuvent être matérialisés UNE seule fois et partagés. La
déduplication inter-multiviews et le sharding (parallélisme) sont la même opération : factoriser
la composition en unités réutilisables.

INVARIANT DE PARTAGE — les pixels d'un nœud à l'instant t = fonction pure de
`(signature de config, entrées dynamiques GLOBALES qu'il lit)`. Les entrées dynamiques (pixels
source, bus tally TSL, niveaux audio, horloge PTP) sont **partagées et identiques pour tous** les
consommateurs. Donc la signature inclut TOUTE la config pixel-déterminante et EXCLUT les valeurs
dynamiques (heure courante, valeur tally, niveau VU) — celles-ci sont précisément ce qui est
partagé. Deux nœuds de même signature lisent donc les mêmes entrées → mêmes pixels → partage sûr.

Cette phase ne fait QUE du calcul (signatures + décomposition + détection des communs) sur des
dicts de config — aucun conteneur, aucune DB, aucun réseau. Entièrement testable hors-ligne.
"""

import hashlib
import json
import logging
import time

log = logging.getLogger(__name__)

# Champs PER-CELLULE qui déterminent les pixels rendus (la POSITION x/y dans le mur n'en fait PAS
# partie — elle relève du layout de la région/sortie, pas des pixels propres de la cellule).
# Signature de cellule : TOUT le cfg SAUF les champs de pure DISPOSITION (liste NOIRE).
# Historique : c'était une liste BLANCHE (_CELL_PIXEL_FIELDS) — 4 instances du même bug en
# 2026-07-12 (flags ANC 0.29.0, ports audio_path/anc_path, puis template/template_ref/
# template_none : changer le modèle de PiP d'une fenêtre shardée ne changeait pas la
# signature → le shard restait STALE, la moitié du mur gardait l'ancien habillage).
# INVERSION du modèle : tout champ, présent ou futur, qui change les pixels est couvert PAR
# DÉFAUT ; on n'exclut que ce qui relève du layout de la sortie (position dans le mur) ou de
# la visibilité (gérée en amont par _visible_cells). Un champ purement informatif qui change
# (rare) coûte au pire une re-matérialisation inutile — jamais un shard périmé silencieux.
# `ratio` = format VOULU de la fenêtre (aspect du modèle de PiP / de la source), référence de
# l'aimant et de « Remplir » côté composer. La cellule est rendue à w×h quoi qu'il arrive : ce
# champ ne change AUCUN pixel, il ne doit donc pas re-matérialiser un shard.
_CELL_LAYOUT_SKIP_FIELDS = ("x", "y", "hidden", "ratio")

# Style GLOBAL du multiview qui affecte le rendu de CHAQUE cellule (donc partie du contexte de
# signature : une cellule n'est partageable entre deux multiviews que s'ils ont le même style).
# default_template : le MODÈLE DE PIP PAR DÉFAUT du mur est du style global — chaque fenêtre
# sans modèle explicite en hérite (résolution script : explicite > défaut du mur > « Classique »
# généré) → il change les pixels de chaque cellule ET doit suivre jusqu'aux shards
# (cf. _mv_params). L'habillage legacy (frame_style/label_size/border_w) a été MIGRÉ dans les
# modèles (multiview 0.33.0) → la liste blanche rétrécit d'autant (classe de bug liste-blanche,
# cf. les 5 variantes du 2026-07-12).
_STYLE_FIELDS = ("chroma", "bit_depth", "colorimetry",
                 "default_template", "default_template_ref")

# Champs d'un overlay à EXCLURE de sa signature : identifiant/position (layout, pas pixels) ; la
# valeur dynamique (heure d'une horloge) n'est jamais un champ de config → exclue naturellement.
_OVERLAY_SKIP_FIELDS = ("id", "x", "y")


def _h(obj):
    """Hash stable et court d'une structure JSON-sérialisable (signature de contenu)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def style_context(params):
    """Sous-ensemble du style global d'un multiview qui influe sur le rendu de chaque cellule."""
    return {k: (params or {}).get(k) for k in _STYLE_FIELDS}


def _norm_path(p):
    """Nom de shm source canonique (sans préfixe /dev/shm/)."""
    p = (p or "").strip()
    return p[len("/dev/shm/"):] if p.startswith("/dev/shm/") else p


def cell_signature(cfg, style):
    """Signature de contenu d'une cellule (1 fenêtre composée) : source + taille + habillage,
    dans le contexte de style global. Indépendante de la position (x/y) dans le mur."""
    body = {"_t": "cell", "style": style}
    for k, v in cfg.items():
        if k in _CELL_LAYOUT_SKIP_FIELDS:
            continue
        # Chemins normalisés (avec ou sans /dev/shm/ → même signature) : path + suiveurs.
        body[k] = _norm_path(v) if k in ("path", "audio_path", "anc_path") else v
    return _h(body)


def overlay_signature(ov, style):
    """Signature de contenu d'un overlay (texte/horloge/image), hors id/position. La valeur
    dynamique (ex. heure affichée) n'est pas une config → naturellement exclue."""
    body = {"_t": "overlay", "style": {"chroma": style.get("chroma"), "bit_depth": style.get("bit_depth")}}
    for k, v in (ov or {}).items():
        if k in _OVERLAY_SKIP_FIELDS:
            continue
        body[k] = v
    return _h(body)


def region_signature(members, region_wh, style):
    """Signature d'une région = bloc rectangulaire d'éléments à positions RELATIVES fixes.
    `members` = liste de (signature_enfant, rel_x, rel_y, w, h) ordonnée canoniquement."""
    canon = sorted([list(m) for m in members])
    return _h({"_t": "region", "members": canon, "wh": list(region_wh), "style": style})


# ─── Décomposition d'un multiview en éléments ────────────────────────────────

def _visible_cells(params):
    """Cellules visibles (non masquées, avec source) d'un multiview, avec leur signature."""
    style = style_context(params)
    out = []
    for i, cfg in enumerate(params.get("flux_config") or []):
        if not isinstance(cfg, dict) or cfg.get("hidden"):
            continue
        if not _norm_path(cfg.get("path")):
            continue
        out.append({
            "idx": i,
            "sig": cell_signature(cfg, style),
            "x": int(cfg.get("x") or 0), "y": int(cfg.get("y") or 0),
            "w": int(cfg.get("w") or 0), "h": int(cfg.get("h") or 0),
            "src": _norm_path(cfg.get("path")),
            "cfg": dict(cfg),                 # config rendable (pour la matérialisation)
        })
    return out


def _overlays(params):
    """Overlays visibles d'un multiview, avec leur signature."""
    style = style_context(params)
    out = []
    for ov in params.get("overlays") or []:
        if not isinstance(ov, dict) or ov.get("hidden"):
            continue
        out.append({
            "id": ov.get("id"),
            "sig": overlay_signature(ov, style),
            "x": int(ov.get("x") or 0), "y": int(ov.get("y") or 0),
            "w": int(ov.get("w") or 0), "h": int(ov.get("h") or 0),
            "kind": ov.get("kind") or "text",
            "ov": dict(ov),                   # config rendable
        })
    return out


def decompose_multiview(params):
    """Décompose un multiview en ses éléments (cellules + overlays) signés, + signature d'output.
    L'output (≈ unique par multiview) est la signature de la région couvrant tout le canvas.

    `meter_blocks` (VU-mètres de MUR, 0.36.0) sont posés en fractions du canvas ENTIER (pas d'une
    cellule) et rendus par `render_meters()` indépendamment des cellules/overlays (cf.
    plugins/multiview/script.py). Ils sont propres à CE mur (pas de notion de partage inter-murs :
    chaque bloc a sa propre source audio câblée) → on les fait transiter TELS QUELS, HORS du
    mécanisme cells/overlays : ni signature, ni entrée dans `members`/`shared_elements` (qui ne
    servent qu'à dédupliquer des ÉLÉMENTS identiques entre plusieurs murs). Les inclure dans une
    signature partagée corromprait un shard partagé entre deux murs (deux murs n'ont jamais les
    mêmes meter_blocks). IDEM `video_history_blocks`/`audio_history_blocks` (frises d'historique
    de MUR, 0.37.0) : mêmes conventions (fractions du canvas entier, source câblée propre)."""
    cells = _visible_cells(params)
    overlays = _overlays(params)
    out_wh = (int(params.get("out_width") or 0), int(params.get("out_height") or 0))
    style = style_context(params)
    members = [(e["sig"], e["x"], e["y"], e["w"], e["h"]) for e in cells + overlays]
    return {
        "cells": cells,
        "overlays": overlays,
        "meter_blocks": list(params.get("meter_blocks") or []),
        "video_history_blocks": list(params.get("video_history_blocks") or []),
        "audio_history_blocks": list(params.get("audio_history_blocks") or []),
        "out_wh": out_wh,
        "output_sig": region_signature(members, out_wh, style),
        "style": style,
    }


# ─── Détection des éléments / blocs COMMUNS à plusieurs multiviews ────────────

def shared_elements(decomps):
    """Éléments (cellules + overlays) dont la SIGNATURE apparaît dans ≥2 multiviews → candidats à
    un nœud partagé. `decomps` = {mv_key: decompose_multiview(...)}. Renvoie {sig: [mv_key, …]}."""
    by_sig = {}
    for key, d in decomps.items():
        seen = set()
        for e in d["cells"] + d["overlays"]:
            if e["sig"] in seen:           # même sig répétée dans le même mv = 1 occurrence mv
                continue
            seen.add(e["sig"])
            by_sig.setdefault(e["sig"], []).append(key)
    return {sig: keys for sig, keys in by_sig.items() if len(keys) >= 2}


def _placements(decomp):
    """{signature → liste de (x, y)} des éléments d'un multiview (pour la détection de blocs)."""
    pl = {}
    for e in decomp["cells"] + decomp["overlays"]:
        pl.setdefault(e["sig"], []).append((e["x"], e["y"]))
    return pl


def shared_block(decomp_a, decomp_b):
    """Plus grand BLOC commun à deux multiviews : ensemble maximal d'éléments de signatures
    identiques disposés selon le MÊME agencement relatif (invariant par translation). Renvoie la
    liste d'éléments du bloc côté A `[(sig, rel_x, rel_y, w, h), …]` (origine relative au coin
    haut-gauche du bloc), ou [] si aucun bloc d'au moins 2 éléments.

    Méthode : pour chaque translation (dx, dy) qui aligne une paire d'éléments de même signature
    entre A et B, compter les éléments de A dont le translaté coïncide (même sig, même position)
    dans B ; retenir la translation au plus grand bloc."""
    a = decomp_a["cells"] + decomp_a["overlays"]
    b = decomp_b["cells"] + decomp_b["overlays"]
    # index B : (sig) → set des positions ; et (sig, x, y) → (w, h)
    b_pos = {}
    for e in b:
        b_pos.setdefault(e["sig"], set()).add((e["x"], e["y"]))
    # éléments de A par signature
    best = []
    tried = set()
    for ea in a:
        for (bx, by) in b_pos.get(ea["sig"], ()):
            dx, dy = bx - ea["x"], by - ea["y"]
            if (dx, dy) in tried:
                continue
            tried.add((dx, dy))
            block = []
            for e in a:
                if (e["x"] + dx, e["y"] + dy) in b_pos.get(e["sig"], ()):
                    block.append(e)
            if len(block) > len(best):
                best = block
    if len(best) < 2:
        return []
    ox = min(e["x"] for e in best)
    oy = min(e["y"] for e in best)
    return sorted((e["sig"], e["x"] - ox, e["y"] - oy, e["w"], e["h"]) for e in best)


# ─── Planificateur (pur) : multiviews → ensemble de nœuds de fabric + assembleurs ────────────
#
# Décide CE QU'IL FAUT matérialiser, sans rien déployer :
#   1. Déduplication : chaque ÉLÉMENT (cellule/overlay) de signature présente sur ≥2 multiviews
#      devient un nœud PARTAGÉ (rendu une fois, lu par tous). Capture « horloge commune », « VU de
#      la source X partagé sur N murs », etc.
#   2. Parallélisme : le RESTE des cellules de chaque multiview est découpé en SHARDS (≤
#      max_cells_per_shard cellules), groupés spatialement → plusieurs process concurrents.
#   3. Assembleur : un nœud par multiview qui TUILE les nœuds (partagés + ses shards + ses overlays
#      résiduels) à leurs offsets dans le mur.
#
# Un nœud est identifié par sa signature et porte sa disposition CANONIQUE (éléments à positions
# RELATIVES à son propre coin, donc indépendante de l'offset dans tel ou tel mur) + sa taille de
# sortie. L'assembleur place la sortie d'un nœud à l'offset (x,y) propre à chaque multiview.

def _even(n):
    """Arrondit au PAIR supérieur (alignement chroma)."""
    n = int(n)
    return n + (n & 1)


def _bbox(elems):
    """Bbox d'un groupe, ALIGNÉE CHROMA : origine ramenée au pair inférieur, dimensions au pair
    supérieur. CRITIQUE : un shard de largeur/hauteur IMPAIRE casse le sous-échantillonnage
    RGBA→YUV de la sortie (pp[:,0::2]+pp[:,1::2] → shapes off-by-one, ex. 881 vs 880) → erreur de
    broadcast à chaque trame. Les murs réels ont des cellules à positions/tailles impaires."""
    x0 = min(e["x"] for e in elems); y0 = min(e["y"] for e in elems)
    x0 -= x0 % 2; y0 -= y0 % 2
    x1 = max(e["x"] + e["w"] for e in elems); y1 = max(e["y"] + e["h"] for e in elems)
    return x0, y0, _even(x1 - x0), _even(y1 - y0)


def _free_cuts(cells, axis):
    """Coupes « guillotine » valides sur un axe ('x'|'y') : une ligne qui ne traverse AUCUNE
    cellule et sépare l'ensemble en deux parts non vides (couloir vide entre cellules). Renvoie
    la liste des (coord, gauche, droite). Garantit l'absence de chevauchement : tout élément de
    `gauche` finit avant la coupe, tout élément de `droite` commence après → bboxes disjointes."""
    lo = "x" if axis == "x" else "y"
    sz = "w" if axis == "x" else "h"
    out = []
    for c in sorted({e[lo] + e[sz] for e in cells}):     # bords 'fin de cellule' = coupes candidates
        left = [e for e in cells if e[lo] + e[sz] <= c]
        right = [e for e in cells if e[lo] >= c]
        if len(left) + len(right) == len(cells) and left and right:   # partition propre, sans straddler
            out.append((c, left, right))
    return out


def _best_cut(cells):
    """Meilleure coupe guillotine (axe, coord, gauche, droite) équilibrant au mieux les DEUX
    parts par aire de bbox, ou None si aucune coupe propre n'existe."""
    best = None
    best_score = None
    for axis in ("x", "y"):
        for c, left, right in _free_cuts(cells, axis):
            al = _bbox(left)
            ar = _bbox(right)
            score = abs(al[2] * al[3] - ar[2] * ar[3])   # |aire_gauche − aire_droite|
            if best_score is None or score < best_score:
                best_score = score
                best = (axis, c, left, right)
    return best


def _guillotine_partition(cells, max_cells, area_budget, force=False):
    """Partitionne `cells` en groupes aux bboxes NON-CHEVAUCHANTES par coupes guillotine
    récursives (split au plus grand couloir vide, le plus équilibré). Chaque groupe finit avec
    ≤ max_cells cellules ET une aire ≤ area_budget tant que c'est découpable. Déterministe et
    LOCALEMENT STABLE (les coupes dépendent des positions, pas d'un index courant : déplacer une
    cellule ne rebrasse que la/les région(s) concernée(s)). Fallback (cellules jointives, aucun
    couloir) : chunk par tri (y,x) — dernier recours rare. INVARIANT : les bboxes des groupes
    renvoyés ne se recouvrent pas (cf. _free_cuts) → l'assembleur peut recopier sans clobber.

    ── Pourquoi la bbox reste SERRÉE (mesuré 2026-08-06) ──────────────────────────────────────
    Donner du mou au rectangle d'un shard (coupes au milieu des couloirs) rendrait un déplacement
    de fenêtre gratuit : même emplacement, même format de sortie → mutation à chaud. Essayé et
    ABANDONNÉ : le mou disponible vaut la MOITIÉ DU COULOIR, soit ~10 px sur un mur dense (les
    couloirs d'un multiview sont minces par construction). Au-delà, la fenêtre chevauche le
    rectangle voisin et il faut re-partitionner quand même. Le mou coûtait +0,8 à +3,8 % d'aire
    rendue sur les murs denses, masquait l'image de fond du mur dans les couloirs (les tuiles sont
    RECOPIÉES sur la toile de fond de l'assembleur), et imposait une re-matérialisation de tous
    les shards existants — pour ne couvrir que les déplacements de moins de 10 px. Le déplacement
    est donc traité en aval : la bascule est rendue INVISIBLE (cf. `pret_fn` dans reconcile_fabric)
    au lieu d'être évitée."""
    bx0, by0, bw, bh = _bbox(cells)
    # `force` : on n'appelle ce partitionneur QUE pour un mur qui SATURE. La condition de feuille
    # (≤ max_cells cellules ET aire ≤ budget) le déclarait pourtant indivisible d'entrée — un mur
    # de 3 fenêtres dont la bbox tient dans la moitié du canevas repartait en UN seul groupe, donc
    # un seul shard, donc aucun parallélisme : le tissu ne pouvait plus rien pour lui, et il
    # plafonnait (26 fps pour 50, mesuré le 2026-08-06 sur le mur 333, alors que DEUX coupes
    # propres existaient). Saturer EST la raison de découper : on impose donc la première coupe
    # quand elle est possible, et les conditions normales reprennent en dessous.
    if not force and len(cells) <= max_cells and bw * bh <= area_budget:
        return [cells]
    cut = _best_cut(cells)
    if cut is None:                       # aucune coupe propre (cellules jointives/chevauchantes)
        s = sorted(cells, key=lambda e: (e["y"], e["x"]))
        return [s[i:i + max_cells] for i in range(0, len(s), max_cells)]
    _axis, _c, left, right = cut
    return (_guillotine_partition(left, max_cells, area_budget)
            + _guillotine_partition(right, max_cells, area_budget))   # `force` : premier niveau seul


def plan_fabric(multiviews, max_cells_per_shard=6, shard_area_frac=0.5):
    """`multiviews` = {key: params}. Renvoie un PLAN pur :
        {"nodes":   {sig: {"kind", "out_wh", "elements": [(elem_sig, relx, rely, w, h)…],
                           "shared_by": [keys]}},
         "outputs": {key: {"tiles": [(node_sig, x, y, w, h)…]}}}
    où chaque `tile` place la sortie d'un nœud à son offset dans le mur `key`. Pur, déterministe."""
    decomps = {k: decompose_multiview(p) for k, p in multiviews.items()}
    shared = shared_elements(decomps)          # {elem_sig: [keys]} présents sur ≥2 multiviews
    nodes = {}
    # chaque output (assembleur) : `tiles` = réfs à des nœuds (shm enfants) placées à leur offset ;
    # `overlays` = overlays NON partagés rendus directement par l'assembleur (cheap, sur place).
    # orientation : portée par l'ASSEMBLEUR seulement (il émet le flux du mur → rotation 90°). Les
    # shards/nœuds composent des régions en portrait LOGIQUE non tourné (cf build_assembler_params).
    # meter_blocks : comme les overlays non partagés, rendus DIRECTEMENT par l'assembleur (cheap,
    # coordonnées déjà en fractions du canvas ENTIER) — jamais partagés/dédupliqués (cf.
    # decompose_multiview), donc simplement recopiés depuis les params logiques du mur.
    outputs = {k: {"tiles": [], "overlays": [], "meter_blocks": decomps[k]["meter_blocks"],
                   "video_history_blocks": decomps[k]["video_history_blocks"],
                   "audio_history_blocks": decomps[k]["audio_history_blocks"],
                   "style": decomps[k]["style"],
                   "out_wh": decomps[k]["out_wh"],
                   "orientation": str((multiviews[k] or {}).get("orientation") or "landscape")}
               for k in multiviews}

    # 1+3a. Éléments PARTAGÉS → un nœud par signature (contenu rendable canonique à l'origine 0,0) ;
    # chaque mur le tuile à SON offset.
    for k, d in decomps.items():
        for e in d["cells"] + d["overlays"]:
            if e["sig"] not in shared:
                continue
            ow, oh = _even(e["w"]), _even(e["h"])    # dims PAIRES (chroma) — cf. _bbox
            if e["sig"] not in nodes:
                n = {"kind": "shared", "out_wh": (ow, oh),
                     "elements": [(e["sig"], 0, 0, ow, oh)],
                     "shared_by": shared[e["sig"]], "windows": [], "overlays": [],
                     "chroma": d["style"].get("chroma"), "bit_depth": d["style"].get("bit_depth"), "default_template": d["style"].get("default_template"), "default_template_ref": d["style"].get("default_template_ref")}
                if "cfg" in e:                       # cellule
                    c = dict(e["cfg"]); c["x"] = 0; c["y"] = 0; n["windows"].append(c)
                else:                                # overlay (ex. horloge)
                    o = dict(e["ov"]); o["x"] = 0; o["y"] = 0; n["overlays"].append(o)
                nodes[e["sig"]] = n
            outputs[k]["tiles"].append((e["sig"], e["x"], e["y"], ow, oh))

    # 2+3b. RESTE de chaque mur → shards de parallélisme (cellules non partagées), partitionnés en
    # régions guillotine NON-CHEVAUCHANTES (cf. _guillotine_partition). Overlays non partagés →
    # rendus directement par l'assembleur (cheap).
    for k, d in decomps.items():
        residual = [e for e in d["cells"] if e["sig"] not in shared]
        if not residual:
            for e in d["overlays"]:
                if e["sig"] not in shared:
                    outputs[k]["overlays"].append(dict(e["ov"]))
            continue
        # Découpage en shards : partition guillotine → bboxes NON-CHEVAUCHANTES, compactes et
        # équilibrées (≤ max_cells_per_shard cellules ET aire ≤ shard_area_frac du mur). Remplace
        # le chunk-par-index (qui produisait des bandes 1920×720 chevauchantes : la tuile la plus
        # basse écrasait en noir une cellule de la tuile du dessus côté assembleur, et le travail
        # memory-bound n'était quasi pas divisé). Aire-budget dérivée des dims du mur (point C).
        _ow, _oh = d["out_wh"]
        _area_budget = max(1, int(_ow) * int(_oh)) * shard_area_frac if (_ow and _oh) else float("inf")
        for group in _guillotine_partition(residual, max_cells_per_shard, _area_budget, force=True):
            ox, oy, bw, bh = _bbox(group)
            members = sorted((e["sig"], e["x"] - ox, e["y"] - oy, e["w"], e["h"]) for e in group)
            nsig = region_signature(members, (bw, bh), d["style"])
            if nsig not in nodes:
                windows = []
                for e in group:
                    c = dict(e["cfg"]); c["x"] = e["x"] - ox; c["y"] = e["y"] - oy
                    windows.append(c)
                nodes[nsig] = {"kind": "shard", "out_wh": (bw, bh), "elements": members,
                               "shared_by": [k], "windows": windows, "overlays": [],
                               "chroma": d["style"].get("chroma"), "bit_depth": d["style"].get("bit_depth"), "default_template": d["style"].get("default_template"), "default_template_ref": d["style"].get("default_template_ref")}
            outputs[k]["tiles"].append((nsig, ox, oy, bw, bh))
        for e in d["overlays"]:
            if e["sig"] not in shared:
                outputs[k]["overlays"].append(dict(e["ov"]))
    return {"nodes": nodes, "outputs": outputs}


# ─── Matérialiseur : plan → conteneurs (deploy/destroy injectables → testable & réutilisable) ──
#
# Chaque nœud du plan devient un conteneur multiview qui REND son contenu (windows/overlays) dans
# un shm `<prefix>_<signature>`. Chaque output (mur logique) devient un ASSEMBLEUR : un multiview
# qui TUILE les sorties des nœuds (copie) à leurs offsets + rend ses overlays non partagés. Le
# registre `fabric_node_alloc` (DB) déduplique (un nœud partagé = 1 conteneur) et porte le cycle de
# vie (teardown des nœuds qui ne sont plus dans le plan).

from .database import db_fabric_get, db_fabric_upsert, db_fabric_touch, db_fabric_delete, db_fabric_all

_RING = 8

# Dernière config d'assembleur RÉELLEMENT poussée à chaque mur : {vmid: (empreinte, instant)}.
# En mémoire de processus, volontairement : au redémarrage de l'orchestrateur on repousse une fois
# (inoffensif) plutôt que de traîner une colonne de plus dans le registre.
# ⚠ REPLI SEULEMENT (voir `_asm_en_place`) : ce chemin mémorise l'empreinte AVANT l'envoi, donc un
# envoi ÉCHOUÉ est retenu comme fait et le mur reste non configuré jusqu'au rafraîchissement.
_asm_pousse = {}
# Plancher de rafraîchissement du repli : même inchangée, la config est re-poussée au moins toutes
# les 10 min. Sans ce filet, un conteneur redémarré en silence (script relancé, rootfs éphémère)
# resterait sans sa config d'assembleur jusqu'à la prochaine VRAIE modification — on échangerait
# une image figée toutes les 35 s contre un mur muet pendant des heures.
_ASM_REFRESH_S = 600

# Reports consécutifs tolérés avant de basculer un mur SANS attendre la cadence de ses nouveaux
# shards (cf. le bloc anti-blocage dans reconcile_fabric). Deux passes = ~2×40 s d'attente réelle :
# au-delà, le témoin de production est plus probablement en panne que le shard.
_differe_ctr = {}
_DIFFERE_MAX = 2

# Passes tolérées où un shard orphelin est encore RAPPORTÉ LU par un mur avant qu'on le détruise
# quand même (cf. étape 3). Trois passes ≈ 1 min 30 : au-delà, c'est le rapport du mur qui est
# suspect, pas le shard.
_tear_ctr = {}
_TEARDOWN_MAX = 3

# ÉTAT VISIBLE du tissu, par mur : {vmid: (etat, instant)} avec etat ∈ {"reorganisation"}.
# Sert UNIQUEMENT à l'interface. Une retouche de contenu est mutée à chaud (quasi instantanée) ;
# un déplacement qui recompose les régions demande un conteneur neuf, donc ~5-10 s avant que la
# sortie bascule. Vu de l'utilisateur, la même action produit tantôt un effet immédiat, tantôt
# une attente inexpliquée. On publie donc l'état pour que l'éditeur puisse l'ANNONCER, au lieu de
# laisser croire à un raté. En mémoire de processus : c'est de l'affichage, pas de la vérité.
_etat_mur = {}


def _marquer_reorganisation(cles):
    """Ces murs attendent un conteneur neuf → la sortie ne suivra pas tout de suite."""
    for k in cles or ():
        _etat_mur[str(k)] = ("reorganisation", time.monotonic())


def etat_mur(vmid):
    """État du tissu pour un mur, à l'usage de l'interface : (etat|None, ancienneté en s)."""
    e = _etat_mur.get(str(vmid))
    if not e:
        return None, 0.0
    return e[0], round(time.monotonic() - e[1], 1)


def _asm_empreinte(asm):
    """Empreinte de la config d'assembleur qu'on s'APPRÊTE à pousser (tous champs confondus)."""
    return hashlib.sha1(json.dumps(asm, sort_keys=True, default=str).encode()).hexdigest()


def _asm_inchange(vmid, asm):
    """Vrai si la config qu'on s'apprête à pousser est EXACTEMENT celle qu'on a poussée en dernier.

    Complément INDISPENSABLE de `_asm_en_place` : ce dernier répond à « le mur a-t-il perdu son
    câblage ? », pas à « avons-nous changé quelque chose ? ». Son témoin ne couvre que le câblage
    des fenêtres et l'ensemble (id, kind) des overlays — volontairement, parce que le mur réécrit
    certains champs et ne publie pas les autres. Résultat, tout ce qui n'est pas dans le témoin
    était SILENCIEUSEMENT ignoré : déplacer une horloge, un VU-mètre de mur ou une frise ne
    repoussait rien, et l'élément ne bougeait jamais sur la sortie (constaté 2026-08-06).
    Pas de plancher de rafraîchissement ici : la question « le mur a-t-il décroché ? » est
    tranchée par l'état OBSERVÉ, pas par un minuteur. Aucune mémoire (redémarrage de
    l'orchestrateur) → on ne saute pas : une poussée de trop est sans effet depuis que les
    endpoints du mur sont idempotents (multiview 0.64.2)."""
    return _asm_pousse.get(vmid, (None,))[0] == _asm_empreinte(asm)


def _asm_deja_pousse(vmid, asm):
    """Vrai si cette config d'assembleur a déjà été poussée à ce mur et reste fraîche.
    Mémorise l'empreinte au passage (l'appelant pousse quand on renvoie False).
    REPLI utilisé quand aucun `etat_fn` n'est fourni — préférer `_asm_en_place`."""
    import time as _t
    emp = hashlib.sha1(json.dumps(asm, sort_keys=True, default=str).encode()).hexdigest()
    vu = _asm_pousse.get(vmid)
    if vu and vu[0] == emp and (_t.monotonic() - vu[1]) < _ASM_REFRESH_S:
        return True
    _asm_pousse[vmid] = (emp, _t.monotonic())
    return False


def _asm_temoin(asm):
    """Signature OBSERVABLE d'une config d'assembleur : ce que le mur doit rapporter s'il est
    réellement câblé ainsi. Deux volets seulement, ceux que le mur publie sur `/state` :
      • le câblage des fenêtres — chemin + géométrie, DANS L'ORDRE (c'est lui qui distingue un
        assembleur, dont les fenêtres lisent les shm `fab_<sig>` des shards, d'un mur redevenu
        monolithique, dont les fenêtres repointent sur les sources d'origine) ;
      • l'ensemble des overlays — (id, kind) triés, pas leur contenu : `text` d'une horloge change
        à chaque seconde et `label` est normalisé côté mur. On détecte une PERTE ou un CHANGEMENT
        d'ensemble, sans être fragile sur des champs que le mur réécrit.
    """
    fen = [(str(w.get("path") or ""), int(w.get("x") or 0), int(w.get("y") or 0),
            int(w.get("w") or 0), int(w.get("h") or 0))
           for w in (asm.get("flux_config") or [])]
    ovl = sorted((str(o.get("id") or ""), str(o.get("kind") or ""))
                 for o in (asm.get("overlays") or []))
    return fen, ovl


def _asm_en_place(etat, asm):
    """Vrai si l'état RAPPORTÉ PAR LE MUR correspond déjà à la config d'assembleur attendue.

    Remplace le rafraîchissement périodique aveugle : un GET `/state` ne coûte AUCUNE recuisson,
    alors que le re-push qu'il évite en provoque une (purge du cache de polices + `overlay_dirty`
    → trame lente → image figée). Et il est STRICTEMENT plus fiable que l'empreinte mémorisée :
      • un conteneur redémarré en silence est détecté à la passe suivante (~30 s) au lieu d'attendre
        le plancher de 10 min ;
      • un envoi ÉCHOUÉ n'est plus retenu comme fait, puisqu'on lit ce qui EST, pas ce qu'on a tenté.

    `etat` = ce que sert le mur sur son `/state`. Toute réponse absente, illisible ou incomplète
    renvoie False : on ne conclut JAMAIS « en place » d'une absence de réponse — le pire cas doit
    être un push inutile, pas un mur laissé sans config.
    """
    if not isinstance(etat, dict) or "windows" not in etat:
        return False
    fen_att, ovl_att = _asm_temoin(asm)
    fen_vu = [(str(w.get("path") or ""), int(w.get("x") or 0), int(w.get("y") or 0),
               int(w.get("w") or 0), int(w.get("h") or 0))
              for w in (etat.get("windows") or [])]
    if fen_vu != fen_att:
        return False
    ovl_vu = sorted((str(o.get("id") or ""), str(o.get("kind") or ""))
                    for o in (etat.get("overlays") or []))
    return ovl_vu == ovl_att


def _mv_params(out_w, out_h, shm_out, flux_config, overlays, fps, chroma, bit_depth,
               genlock=True, cadence="input", scan=None,
               slice_mode=False, slice_lines=36, default_template=None, default_template_ref=""):
    # cadence="input" par défaut : les nœuds du tissu sont DATA-DRIVEN (suivent l'entrée, pas la
    # grille) → latence cumulée du DAG = Σ calcul, pas N×intervalle (cf. plugin INPUT_LOCKED).
    # L'habillage vit dans les MODÈLES DE PIP (embarqués par cellule dans flux_config, +
    # default_template ci-dessous) — plus aucun champ d'habillage global de mur.
    # TISSU EN TRANCHES (docs/chantiers/TISSU_SLICE.md) : slice_mode=True → nœuds/assembleurs en cadence "flow"
    # (data-flow aligné sur la grille TAI : composition ciblée sur l'index d'epoch, sortie écrite
    # au même index → alignement inter-étages) + publication bande par bande. Défaut OFF →
    # params STRICTEMENT identiques à l'historique (cadence "input", pas de clés slice).
    out = {"out_width": int(out_w), "out_height": int(out_h), "chroma": chroma,
           "bit_depth": bit_depth, "shm_video_ring": _RING, "fps": fps, "genlock": genlock,
           "cadence": ("flow" if slice_mode else cadence), "shm_out": shm_out,
           "max_inputs": 0, "flux_config": flux_config, "overlays": overlays}
    if slice_mode:
        out["slice_mode"] = True
        out["slice_lines"] = int(slice_lines or 36)
    # Modèle de PiP PAR DÉFAUT du mur : hérité par les fenêtres des SHARDS (résolution script :
    # explicite > défaut du mur > « Classique » généré). L'ASSEMBLEUR, lui, ne le reçoit
    # PAS (build_assembler_params ne le passe pas) : ses fenêtres sont des shards pré-rendus
    # posés 1:1 (show_label/show_tally faux → « Classique » généré = vidéo nue = copie pure) —
    # lui appliquer un modèle re-doublerait l'habillage par-dessus les pixels.
    # ★ SCAN EXPLICITE. Ne PAS omettre cette clé : `plugins.render_script` comble toute clé de
    # format absente d'un multiview avec le FORMAT DE SORTIE PAR DÉFAUT DU SYSTÈME
    # (`scripts.multiview_output_format_defaults`). Un nœud de tissu sans `scan` héritait donc du
    # scan du site — sur un site en 1080i50, les liens INTERNES du tissu partaient en entrelacé.
    # Cf. build_node_params pour ce que ça coûtait.
    if scan:
        out["scan"] = scan
    if default_template is not None:
        out["default_template"] = default_template
        out["default_template_ref"] = default_template_ref or ""
    return out


def build_node_params(node, shm, fps=50, chroma=None, bit_depth=None, slice_mode=False):
    """Params multiview d'un NŒUD (shard ou élément partagé) : rend ses windows/overlays (positions
    relatives à son coin) dans `shm`. HÉRITE du format du mur (node['chroma']/['bit_depth']) pour ne
    PAS dégrader le 4:2:2/10-bit en 4:2:0/8-bit ; fallback "422"/8 = défaut du script multiview."""
    ow, oh = node["out_wh"]
    ch = node.get("chroma") or chroma or "422"
    bd = node.get("bit_depth") or bit_depth or 8
    # Shard : reproduit l'habillage RÉEL du mur (modèles embarqués par cellule + modèle par
    # défaut du mur) → l'assembleur n'a plus qu'à recopier.
    # ★ LIEN INTERNE = TOUJOURS PROGRESSIF. Un shard COMPOSE en progressif ; le laisser ÉMETTRE en
    # entrelacé (ce qui arrivait dès que le format par défaut du site était en « i », cf. _mv_params)
    # faisait découper sa trame en deux champs, que l'assembleur relisait comme une source
    # entrelacée — donc en n'en prenant QU'UN. La moitié de la résolution verticale du mur était
    # jetée au tout dernier étage, après le filtrage et le désentrelacement des sources (constaté
    # sur trame capturée : texte d'UMD en marches de 2 px). Découper puis retisser un lien interne
    # est de toute façon du travail pur : trois étages pour revenir au point de départ.
    # L'entrelacement n'a de sens que sur la sortie RÉELLE du mur (l'assembleur), pas entre deux
    # étages de calcul du même nœud.
    return _mv_params(ow, oh, shm, list(node.get("windows") or []),
                      list(node.get("overlays") or []), fps, ch, bd, slice_mode=slice_mode,
                      scan="p",
                      default_template=node.get("default_template"),
                      default_template_ref=node.get("default_template_ref") or "")


def build_assembler_params(output, shm_out, sig_to_shm, fps=50, chroma=None, bit_depth=None,
                           slice_mode=False):
    """Params multiview de l'ASSEMBLEUR d'un mur : une fenêtre par tuile lisant le shm du nœud
    enfant (copie pure, pas de label/VU) + les overlays non partagés rendus sur place. HÉRITE du
    format du mur (output['style']) — même chroma/bit_depth que les shards (sinon mauvaise lecture)."""
    ow, oh = output["out_wh"]
    _stl = output.get("style") or {}
    chroma = _stl.get("chroma") or chroma or "422"
    bit_depth = _stl.get("bit_depth") or bit_depth or 8
    fc = []
    for (nsig, x, y, w, h) in output["tiles"]:
        child = sig_to_shm.get(nsig)
        if not child:
            continue
        fc.append({"path": "/dev/shm/" + child, "name": "", "x": int(x), "y": int(y),
                   "w": int(w), "h": int(h), "in_w": int(w), "in_h": int(h),
                   "show_label": False, "show_tally": False, "tsl_index": 0,
                   "label_source": "hostname", "meter_channels": 0, "meter_position": "right",
                   "meter_inside": False, "meter_opacity": 70, "meter_scale": "dbfs"})
    # Assembleur = COPIE PURE des sorties de shards (qui portent déjà l'habillage du mur) → AUCUN
    # chrome propre : pas de default_template, fenêtres show_label/show_tally faux → modèle
    # « Classique » généré = vidéo nue → _chrome_pre None → pas de blend_pre plein écran à
    # chaque trame (c'était ~13 ms, le goulet après le sharding). Seuls les overlays non
    # partagés (horloges) sont composés sur place.
    params = _mv_params(ow, oh, shm_out, fc, list(output.get("overlays") or []),
                        fps, chroma, bit_depth, slice_mode=slice_mode)
    # L'assembleur émet le flux EXTERNE du mur → c'est lui qui tourne 90° en portrait (les shards
    # restent non tournés). out_width/out_height = canevas portrait logique ; le moteur swappe à l'émission.
    params["orientation"] = str(output.get("orientation") or "landscape")
    # meter_blocks : VU-mètres de MUR — recopiés TELS QUELS (fractions du canvas entier, déjà dans
    # le bon référentiel ; le moteur sait les rendre indépendamment du rôle assembleur/monolithe,
    # cf. render_meters). Pas de résolution de dims/proxy (juste une source audio par bloc, comme
    # _multiview_hot_apply). L'assembleur reste le MÊME conteneur/vmid que le mur logique (jamais
    # un nouveau conteneur créé) → le câblage audio_path (shm local au nœud) reste valide tel quel.
    params["meter_blocks"] = list(output.get("meter_blocks") or [])
    # Frises d'historique de MUR (0.37.0) : même transit tel quel que meter_blocks ci-dessus.
    params["video_history_blocks"] = list(output.get("video_history_blocks") or [])
    params["audio_history_blocks"] = list(output.get("audio_history_blocks") or [])
    return params


def materialize(plan, deploy_fn, destroy_fn, shm_out_by_key, fps=50, chroma=None,
                bit_depth=None, name_prefix="fab", slice_mode=False):
    """Réalise un plan_fabric en conteneurs.
      deploy_fn(name, params, hostname)  → (dé)ploie un multiview (idempotent côté appelant).
      destroy_fn(name)                   → détruit un conteneur.
      shm_out_by_key                     → {mv_key: shm de sortie du mur logique}.
    Déduplique via le registre (un nœud déjà matérialisé n'est pas recréé) et détruit les nœuds
    orphelins (plus dans le plan). Renvoie {"nodes_created":[…], "nodes_kept":[…], "torn_down":[…]}."""
    res = {"nodes_created": [], "nodes_kept": [], "torn_down": []}
    sig_to_shm = {}
    # 1. Nœuds (shards + partagés)
    for sig, node in plan["nodes"].items():
        shm = f"{name_prefix}_{sig}"
        sig_to_shm[sig] = shm
        if db_fabric_get(sig):
            db_fabric_touch([sig]); res["nodes_kept"].append(sig); continue
        params = build_node_params(node, shm, fps, chroma, bit_depth, slice_mode=slice_mode)
        name = f"bobi-{name_prefix}-{sig}"
        ref = deploy_fn(name, params, name) or name
        ow, oh = node["out_wh"]
        db_fabric_upsert(sig, None, None, shm, node["kind"], int(ow), int(oh),
                         ref=str(ref), parents=node.get("shared_by"))
        res["nodes_created"].append(sig)
    # 2. Assembleurs (un par mur logique)
    for key, output in plan["outputs"].items():
        params = build_assembler_params(output, shm_out_by_key[key], sig_to_shm, fps, chroma,
                                        bit_depth, slice_mode=slice_mode)
        deploy_fn(f"bobi-{name_prefix}-asm-{key}", params, f"asm-{key}")
    # 3. Teardown des nœuds orphelins (plus dans le plan)
    for row in db_fabric_all():
        if row["signature"] not in plan["nodes"]:
            try: destroy_fn(f"bobi-{name_prefix}-{row['signature']}")
            except Exception: pass
            db_fabric_delete(row["signature"])
            res["torn_down"].append(row["signature"])
    return res


# ─── Auto-trigger : compiler les multiviews SATURÉS en fabric (réactif, calqué reconcile pyramide) ──
#
# Un multiview qui sature (own_latency mesuré > budget de trame) ET qui a au moins 2 tuiles câblées à
# répartir (sinon rien à paralléliser) → on matérialise ses shards (+ dédup avec les autres multiviews
# lourds) et on RECONFIGURE son conteneur en ASSEMBLEUR (hot, même shm_out → transparent pour l'aval).
# Un multiview qui ne sature plus → restauré en monolithe + ses shards exclusifs détruits. État
# « assembleur » suivi par une ligne registre `asm:<vmid>` (kind='assembler').
#
# Le déclenchement est piloté par la SATURATION (own_latency > budget), pas par un nombre de fenêtres
# fixe : un mur saturé à 5 tuiles doit être shardé. `min_shard_cells` (défaut 2) n'est qu'un plancher
# de « splittabilité » — il faut ≥2 tuiles pour répartir sur des shards parallèles.

def _n_visible(params):
    return sum(1 for c in (params.get("flux_config") or [])
               if isinstance(c, dict) and not c.get("hidden") and _norm_path(c.get("path")))


# ─── Emplacements : l'identité STABLE d'un shard, par opposition à sa signature de contenu ───
#
# Un nœud du tissu est adressé par son CONTENU (signature) — c'est ce qui rend la déduplication et
# le partage entre murs corrects par construction. Mais un conteneur, lui, est une ressource
# COÛTEUSE à créer : quelques secondes de boot pendant lesquelles la région du mur est vide. Or la
# très grande majorité des re-planifications ne déplacent rien : elles changent le contenu d'une
# région qui reste au même endroit, à la même taille, au même format. Ces trois-là forment
# l'EMPLACEMENT — la partie de l'identité d'un shard que l'assembleur observe (il ne connaît qu'un
# shm, une position et une taille). Tant que l'emplacement ne bouge pas, on peut remplacer le
# contenu du conteneur sans que rien en aval ne s'en aperçoive.

def _node_fmt(node, fps, chroma, bit_depth, slice_mode):
    """Empreinte du FORMAT de sortie d'un nœud — ce qu'un `/reconfigure` à chaud ne peut PAS
    changer (le flux MXL de sortie est déjà créé). Mêmes replis que `build_node_params`."""
    return _h({"chroma": node.get("chroma") or chroma or "422",
               "bit_depth": node.get("bit_depth") or bit_depth or 8,
               "fps": fps, "slice": bool(slice_mode)})


def _emplacements_liberes(node_id, want):
    """Shards du nœud qui SORTENT du plan courant, indexés par emplacement. Uniquement ceux à
    parent UNIQUE : un nœud partagé entre deux murs ne peut pas être muté sans toucher l'autre."""
    libres = {}
    for row in db_fabric_all(node_id):
        if row["kind"] != "shard" or row["signature"] in want or not row["ref"]:
            continue
        try:
            par = json.loads(row["parents"]) if row["parents"] else []
        except Exception:                                                  # noqa: BLE001
            continue
        if len(par) != 1 or row["tile_x"] is None or not row["fmt"]:
            continue   # ligne d'avant l'introduction des emplacements → pas de rebind possible
        cle = (str(par[0]), int(row["tile_x"]), int(row["tile_y"] or 0),
               int(row["out_w"] or 0), int(row["out_h"] or 0), str(row["fmt"]))
        libres.setdefault(cle, []).append(dict(row))
    return libres


def _cle_emplacement(place, fmt):
    k, x, y, w, h = place
    return (str(k), int(x), int(y), int(w), int(h), str(fmt))


def _prendre_emplacement(libres, place, fmt):
    """Retire et renvoie un emplacement libre correspondant, ou None."""
    f = libres.get(_cle_emplacement(place, fmt))
    return f.pop() if f else None


def _rendre_emplacement(libres, row):
    """Remet un emplacement dans le pot (rebind refusé/échoué) — il redeviendra un teardown."""
    try:
        par = json.loads(row["parents"]) if row["parents"] else []
    except Exception:                                                      # noqa: BLE001
        return
    if len(par) != 1:
        return
    cle = (str(par[0]), int(row["tile_x"]), int(row["tile_y"] or 0),
           int(row["out_w"] or 0), int(row["out_h"] or 0), str(row["fmt"]))
    libres.setdefault(cle, []).append(row)


def reconcile_fabric(node_id, mvs, latency_ms, deploy_fn, destroy_fn, reconfigure_fn, restore_fn,
                     budget_ms=20.0, min_shard_cells=2, max_cells_per_shard=4,
                     fps=50, chroma=None, bit_depth=None, name_prefix="fab", budget_by_vmid=None,
                     slice_mode=False, etat_fn=None, rebind_fn=None, pret_fn=None,
                     max_noeuds=None, perime_fn=None, restaurables=None):
    """mvs={vmid: logical_params}, latency_ms={vmid: own_latency_ms|None}. Callbacks :
      deploy_fn(name, params, hostname)   crée un conteneur NŒUD (shard) ;
      destroy_fn(name)                    le détruit ;
      rebind_fn(ref, params) -> bool      REMPLACE À CHAUD le contenu d'un shard existant (même
                                          conteneur, même shm de sortie). False = refus/échec →
                                          on retombe sur créer+détruire ;
      pret_fn(refs) -> set                attend que des shards NEUFS produisent réellement leur
                                          première trame ; renvoie ceux qui sont prêts ;
      perime_fn() -> bool                 « cette réplanification est-elle déjà dépassée ? ».
                                          Vrai → on renonce AUX POINTS SÛRS (avant de créer un
                                          conteneur, pendant l'attente de production, avant la
                                          bascule) plutôt que d'aller au bout d'un plan périmé.
    `max_noeuds` = nombre de shards que le NŒUD peut réellement épingler (cœurs libres du pool ÷
    profil du type). Au-delà, la découpe est RÉ-AGRÉGÉE (groupes plus gros) : mieux vaut trois
    shards épinglés que six qui se disputent les mêmes cœurs physiques.
      reconfigure_fn(vmid, asm_params)    reconfigure le multiview vmid en ASSEMBLEUR (hot) ;
      restore_fn(vmid, logical_params)    le restaure en monolithe (hot) ;
      etat_fn(vmid) -> dict|None          lit l'état RAPPORTÉ par le mur (`/state`), sans effet de
                                          bord. Fourni → on ne repousse qu'en cas de divergence
                                          réelle (cf. `_asm_en_place`) ; absent → repli sur
                                          l'empreinte mémorisée + rafraîchissement 10 min.
    `budget_by_vmid` = budget de trame (ms) PAR multiview (intention de cadence ; sinon budget_ms
    global). Dédup inter-multiviews (plan commun des lourds). Idempotent. Renvoie un résumé."""
    budget_by_vmid = budget_by_vmid or {}
    res = {"sharded": [], "restored": [], "nodes_created": [], "torn_down": []}
    heavy = {}
    for vmid, p in mvs.items():
        nwin = _n_visible(p)
        already = db_fabric_get(f"asm:{vmid}") is not None
        if already:
            # DÉJÀ shardé : la latence mesurée est celle de l'ASSEMBLEUR (basse car shardé) → ne PAS
            # s'en servir pour décider (sinon flap shard↔restore). Deux sorties, et deux seulement :
            #
            #  - STRUCTURELLE : plus assez de tuiles câblées pour paralléliser.
            #  - ★ ÉCONOMIQUE (`restaurables`) : le découpage ne rapporte PLUS. Le critère ne peut pas
            #    être la latence de l'assembleur ; c'est l'orchestrateur qui l'établit, en sommant le
            #    coût des shards (majorant du monolithe) et en exigeant une MARGE et une PERSISTANCE
            #    — cf. `deploy._restaurables_tissu`. Sans cette sortie, un mur shardé par accident ne
            #    redevenait JAMAIS monolithe : le critère était le NOMBRE DE TUILES, jamais le gain.
            #    Vécu le 2026-08-08 : un simple redéploiement a fait passer le mur 906 de 3 à 9 cœurs
            #    et 3 processus, définitivement, pour un travail qu'il tenait à 3.
            if nwin >= min_shard_cells and vmid not in (restaurables or ()):
                heavy[vmid] = p
            else:
                restore_fn(vmid, p)
                db_fabric_delete(f"asm:{vmid}")
                res["restored"].append(vmid)
        else:
            # PAS encore shardé : la SATURATION pilote le déclenchement (own_latency du monolithe vs
            # budget PROPRE au multiview — intention de cadence — sinon budget global). min_shard_cells
            # = simple plancher de splittabilité (il faut ≥2 tuiles pour des shards parallèles), PAS un
            # seuil de taille : un mur saturé à 5 tuiles doit être shardé.
            lat = latency_ms.get(vmid)
            _budget = budget_by_vmid.get(vmid, budget_ms)
            if lat is not None and lat > _budget and nwin >= min_shard_cells:
                heavy[vmid] = p
    plan = (plan_fabric({str(v): p for v, p in heavy.items()}, max_cells_per_shard)
            if heavy else {"nodes": {}, "outputs": {}})
    # ── SHARDER EN UNE SEULE TUILE NE SERT À RIEN ────────────────────────────────────────────
    # La saturation décide de sharder, mais elle ne dit pas que la découpe SERA parallèle. Un mur
    # dont le plan ne produit qu'UNE tuile ne gagne aucun parallélisme : tout le travail reste
    # dans un seul conteneur, et on lui ajoute une recopie plein cadre chez l'assembleur, un
    # conteneur de plus et un étage de latence. C'est STRICTEMENT pire que le monolithe.
    # Mesuré le 2026-08-06 sur le mur 333 (3 fenêtres, dont deux de 832×482) :
    #   monolithe          33 fps, own 27 ms
    #   1 shard + assembleur  25 fps VISIBLES (shard 27,2 ms dont 21,4 de gather ; assembleur 7,3)
    # Le mur restait shardé en boucle parce que la sortie de sharding se décide sur le NOMBRE DE
    # TUILES (≥ 2 → on reste) et non sur le gain réel. Le parallélisme exige au moins deux nœuds ;
    # en dessous, on garde — ou on restaure — le monolithe.
    _restaures = set()      # murs remis en monolithe à CETTE passe (à interroger avant teardown)
    # ── LA DÉCOUPE S'ADAPTE À LA MACHINE ────────────────────────────────────────────────────
    # Le tissu décidait du nombre de shards sans jamais demander si le nœud pouvait les ÉPINGLER.
    # Constaté le 2026-08-07 sur dl360-1 : pool de 6 cœurs physiques, un mur + deux shards à 3
    # cœurs chacun → `physical_free = 0`, `oversub = true`, et un shard placé sur les jumeaux HT
    # du mur lui-même (ils se disputent le même cœur physique). Plutôt que de refuser de sharder —
    # le monolithe saturé est MESURÉ pire (33 fps contre 50) — on ré-agrège : des groupes plus
    # gros, donc moins de conteneurs, jusqu'à tenir dans ce que le nœud sait épingler.
    if max_noeuds and heavy and len(plan["nodes"]) > max_noeuds:
        _mc = max_cells_per_shard
        while len(plan["nodes"]) > max_noeuds and _mc < 64:
            _mc *= 2
            plan = plan_fabric({str(v): p for v, p in heavy.items()}, _mc)
        log.info("tissu : découpe ré-agrégée à %d nœud(s) (max_cells %d → %d) — le nœud n'en "
                 "épingle que %d", len(plan["nodes"]), max_cells_per_shard, _mc, max_noeuds)
        if len(plan["nodes"]) > max_noeuds:
            # Irréductible (le parallélisme minimal dépasse déjà la capacité) : on shard quand
            # même, mais l'exploitant doit savoir que ces shards ne seront pas épinglés seuls.
            log.warning("tissu : %d nœud(s) planifiés pour %d épinglables — le nœud est "
                        "sur-souscrit, les shards partageront des cœurs physiques",
                        len(plan["nodes"]), max_noeuds)
            res.setdefault("sur_souscrit", []).append(node_id)
    _solos = [v for v in heavy
              if len((plan["outputs"].get(str(v)) or {}).get("tiles") or []) < 2]
    for _v in _solos:
        _p = heavy.pop(_v)
        if db_fabric_get(f"asm:{_v}"):
            restore_fn(_v, _p)
            db_fabric_delete(f"asm:{_v}")
            _asm_pousse.pop(_v, None)
            _etat_mur.pop(str(_v), None)
            res["restored"].append(_v)
            _restaures.add(_v)
            log.info("tissu : mur %s — la découpe ne donne qu'une tuile, aucun parallélisme : "
                     "retour au monolithe", _v)
        res.setdefault("sharding_sans_gain", []).append(_v)
    if _solos:
        plan = (plan_fabric({str(v): p for v, p in heavy.items()}, max_cells_per_shard)
                if heavy else {"nodes": {}, "outputs": {}})
    want = set(plan["nodes"])
    # EMPLACEMENT de chaque nœud dans son mur : sig → [(mv_key, x, y, w, h)] (cf. rebind).
    places = {}
    for _k, _out in plan["outputs"].items():
        for (_ns, _x, _y, _w, _h) in _out["tiles"]:
            places.setdefault(_ns, []).append((str(_k), int(_x), int(_y), int(_w), int(_h)))
    # Nœuds qui SORTENT du plan, indexés par emplacement : candidats à la mutation à chaud.
    libres = _emplacements_liberes(node_id, want) if rebind_fn is not None else {}
    # 1. matérialiser les NŒUDS (shards/partagés) manquants ; dédup via registre
    sig_to_shm = {}
    shm_to_ref = {}     # {shm de sortie: ref du conteneur} — sert au contrôle de production
    for sig, node in plan["nodes"].items():
        row = db_fabric_get(sig)
        if row:
            # shm du REGISTRE et non `prefix_sig` : un nœud REBINDÉ garde le shm de l'emplacement
            # qu'il occupe (c'est ce qui laisse le câblage de l'assembleur intact), donc le nom ne
            # se dérive plus de la signature.
            sig_to_shm[sig] = row["shm"] or f"{name_prefix}_{sig}"
            shm_to_ref[sig_to_shm[sig]] = row["ref"]
            db_fabric_touch([sig]); continue
        ow, oh = node["out_wh"]
        fmt = _node_fmt(node, fps, chroma, bit_depth, slice_mode)
        # ── REBIND : même emplacement, même taille, même format, contenu différent ──────────
        # Sans lui, la moindre retouche d'une cellule (un libellé, un modèle de PiP, une source)
        # change la signature de la région → conteneur DÉTRUIT et REMPLACÉ : plusieurs secondes de
        # boot pendant lesquelles l'assembleur pointe un shm qui n'existe pas encore (région
        # noire), plus une reconfiguration de l'assembleur (recuisson des overlays) et deux
        # conteneurs rendant la même région le temps du recouvrement. Muter le shard en place ne
        # coûte RIEN de visible : même conteneur, même shm, l'assembleur n'est même pas touché.
        # Réservé aux shards à parent UNIQUE : un nœud PARTAGÉ entre deux murs ne peut pas être
        # muté (on changerait aussi les pixels de l'autre mur) — il est forké, comme aujourd'hui.
        _pl = places.get(sig) or []
        _cand = None
        if (rebind_fn is not None and node["kind"] == "shard" and len(_pl) == 1
                and len(node.get("shared_by") or []) == 1):
            _cand = _prendre_emplacement(libres, _pl[0], fmt)
        if _cand is not None:
            _shm = _cand["shm"]
            if rebind_fn(_cand["ref"], build_node_params(node, _shm, fps, chroma, bit_depth,
                                                         slice_mode=slice_mode)):
                _, _tx, _ty, _, _ = _pl[0]
                db_fabric_delete(_cand["signature"])
                db_fabric_upsert(sig, node_id, None, _shm, node["kind"], int(ow), int(oh),
                                 ref=_cand["ref"], parents=node.get("shared_by"),
                                 tile_x=_tx, tile_y=_ty, fmt=fmt)
                sig_to_shm[sig] = _shm
                shm_to_ref[_shm] = _cand["ref"]
                res.setdefault("rebound", []).append(sig)
                continue
            # Refus ou échec du push à chaud → on REND l'emplacement et on repart sur
            # créer+détruire. Jamais un shard laissé à un contenu périmé en silence.
            _rendre_emplacement(libres, _cand)
        shm = f"{name_prefix}_{sig}"; sig_to_shm[sig] = shm
        if perime_fn is not None and perime_fn():
            log.info("tissu : création de %s abandonnée — édition plus récente en attente", sig)
            res.setdefault("abandonne", []).append(sig)
            return res
        _name = f"bobi-{name_prefix}-{sig}"
        # ⚠ AVANT `deploy_fn`, pas après. La création du conteneur + le déploiement de son script
        # prennent ~3 s : marquer ensuite faisait apparaître « Réorganisation du mur » alors que
        # l'essentiel de l'attente était déjà passé — un indicateur qui arrive à la fin n'informe
        # personne. Ici, on vient de DÉCIDER de créer (rebind impossible ou refusé) : c'est le
        # premier instant où l'on sait que ce sera lent, donc le bon moment pour le dire.
        _marquer_reorganisation(node.get("shared_by"))
        ref = deploy_fn(_name, build_node_params(node, shm, fps, chroma, bit_depth,
                                                 slice_mode=slice_mode), _name) or _name
        _tx, _ty = (_pl[0][1], _pl[0][2]) if len(_pl) == 1 else (None, None)
        db_fabric_upsert(sig, node_id, None, shm, node["kind"], int(ow), int(oh),
                         ref=str(ref), parents=node.get("shared_by"),
                         tile_x=_tx, tile_y=_ty, fmt=fmt)
        shm_to_ref[shm] = str(ref)
        res["nodes_created"].append(sig)
    # 2. reconfigurer chaque multiview lourd en assembleur (même shm_out)
    differes = set()
    for vmid, p in heavy.items():
        asm = build_assembler_params(plan["outputs"][str(vmid)], p.get("shm_out"),
                                     sig_to_shm, fps, chroma, bit_depth, slice_mode=slice_mode)
        # IDEMPOTENCE DE L'ENVOI — pas seulement du registre. Cette boucle est appelée toutes les
        # ~30 s par la surveillance (main.py), et elle re-poussait /style + /overlays +
        # /reconfigure À CHAQUE PASSE même quand rien n'avait changé. Or côté mur chaque
        # /reconfigure purge le cache de polices et lève `overlay_dirty` → RECUISSON complète des
        # overlays → une trame lente (25-47 ms mesurées) → le TX ne trouve pas de grain neuf et
        # ré-émet le précédent : une image figée toutes les ~34,7 s, en production, sans que rien
        # n'ait bougé. Diagnostiqué sur Horace le 2026-08-05 (la période des `[fonts]` du mur, celle
        # des commits tardifs et le 5 s × 6 de la boucle coïncidaient exactement).
        # Le garde-fou `if not db_fabric_get(...)` juste dessous ne protégeait que l'ÉCRITURE EN
        # BASE, et il vient APRÈS l'envoi : l'idempotence était intentionnelle mais pas effective.
        # ÉTAT OBSERVÉ plutôt que minuteur aveugle : on interroge le mur (`/state`, aucun coût de
        # recuisson) et on ne repousse que s'il ne rapporte PAS déjà le câblage attendu. Le
        # rafraîchissement périodique qu'on remplace, lui, provoquait la recuisson qu'il fallait
        # éviter — 2 images figées par 20 min sur un mur shardé, mesurées à Horace.
        # Un `etat_fn` qui lève ne doit JAMAIS interrompre la réconciliation des autres murs : on
        # retombe sur « état inconnu » → False → on repousse (le pire cas reste un push inutile).
        # ⚠ DEUX questions, pas une : « le mur a-t-il perdu son câblage ? » (état observé) ET
        # « avons-nous changé quelque chose ? » (empreinte de ce qu'on s'apprête à pousser). Le
        # témoin d'état ne couvre PAS les overlays au-delà de (id, kind), ni les VU-mètres, ni les
        # frises — s'y fier seul rendait un déplacement d'horloge sans effet (cf. `_asm_inchange`).
        _etat = None
        if etat_fn is not None:
            try:
                _etat = etat_fn(vmid)
                _saute = _asm_en_place(_etat, asm) and _asm_inchange(vmid, asm)
            except Exception as _ee:                                        # noqa: BLE001
                log.warning("tissu : état du mur %s illisible (%s) — on repousse", vmid, _ee)
                _saute = False
        else:
            _saute = _asm_deja_pousse(vmid, asm)
        if _saute:
            db_fabric_touch([f"asm:{vmid}"])
            _etat_mur.pop(str(vmid), None)
            res.setdefault("sharded_inchanges", []).append(vmid)
            continue
        # ── BASCULE INVISIBLE (déplacement de fenêtre) ─────────────────────────────────────
        # Déplacer une fenêtre change la découpe : il FAUT de nouveaux conteneurs, on ne peut pas
        # l'éviter (cf. _guillotine_partition). Mais rien n'oblige à ce que ça se VOIE. L'assembleur
        # était repointé aussitôt après la création, sur un shm que le conteneur n'avait pas encore
        # créé : la région restait noire pendant tout le boot (docker run + démarrage du script +
        # création du flux MXL), puis se rallumait — et l'ancien shard était détruit dans la foulée.
        # On exige désormais que TOUT shm vers lequel le mur ne pointe pas déjà soit RÉELLEMENT en
        # production avant de basculer. Sinon on ne touche ni l'assembleur ni les anciens shards
        # (cf. étape 3) : le mur continue d'afficher sa composition actuelle, et la bascule se fait
        # d'un coup quand elle est prête. Le critère porte sur « ce vers quoi on va basculer », pas
        # sur « ce qu'on vient de créer » : un shard encore muet à la passe suivante reste couvert.
        if pret_fn is not None:
            _vus = {str(w.get("path") or "") for w in ((_etat or {}).get("windows") or [])}
            _refs = {shm_to_ref.get(_norm_path(w.get("path")))
                     for w in (asm.get("flux_config") or [])
                     if str(w.get("path") or "") not in _vus}
            _refs.discard(None)
            if _refs:
                _prets = pret_fn(sorted(_refs)) or set()
                _muets = sorted(r for r in _refs if r not in _prets)
                # ANTI-BLOCAGE — un contrôle de SÛRETÉ ne doit jamais pouvoir figer le système.
                # Différer indéfiniment, c'est « plus rien ne se met à jour », et c'est
                # exactement ce qui est arrivé le 2026-08-06 : le témoin de production lisait une
                # clé que le multiview ne publie pas, donc AUCUN shard n'était jamais déclaré
                # prêt. Au-delà de _DIFFERE_MAX reports consécutifs, on bascule quand même (on
                # retrouve le comportement d'avant : au pire une région noire transitoire) et on
                # ALERTE, plutôt que de laisser le mur muet sans que personne ne le sache.
                if _muets and _differe_ctr.get(vmid, 0) >= _DIFFERE_MAX:
                    log.warning("tissu : mur %s — %d report(s) consécutif(s), shard(s) %s toujours "
                                "sans cadence : on bascule quand même", vmid,
                                _differe_ctr.get(vmid, 0), _muets)
                    _differe_ctr.pop(vmid, None)
                    res.setdefault("bascules_forcees", []).append(vmid)
                elif _muets:
                    differes.add(str(vmid))
                    _differe_ctr[vmid] = _differe_ctr.get(vmid, 0) + 1
                    # Le repli par empreinte mémorise AVANT l'envoi (cf. `_asm_deja_pousse`) : sans
                    # cet oubli, une bascule différée serait retenue comme faite et le mur ne
                    # basculerait JAMAIS. Sans effet sur le chemin nominal (état observé).
                    _asm_pousse.pop(vmid, None)
                    res.setdefault("differes", []).append(vmid)
                    log.info("tissu : mur %s — bascule différée (%d), shard(s) %s pas encore en "
                             "production", vmid, _differe_ctr[vmid], _muets)
                    continue
        if perime_fn is not None and perime_fn():
            log.info("tissu : bascule du mur %s abandonnée — édition plus récente en attente", vmid)
            res.setdefault("abandonne", []).append(vmid)
            return res
        _differe_ctr.pop(vmid, None)
        reconfigure_fn(vmid, asm)
        # Empreinte mémorisée APRÈS l'envoi (le repli historique le faisait AVANT, si bien qu'un
        # envoi échoué était retenu comme fait et le mur restait sur l'ancienne config).
        _asm_pousse[vmid] = (_asm_empreinte(asm), time.monotonic())
        _etat_mur.pop(str(vmid), None)      # la sortie a basculé : plus rien à annoncer
        if not db_fabric_get(f"asm:{vmid}"):
            db_fabric_upsert(f"asm:{vmid}", node_id, vmid, p.get("shm_out") or "", "assembler", 0, 0)
        res["sharded"].append(vmid)
    # 3. teardown des nœuds (shard/partagé) orphelins (plus dans le plan courant). Les nœuds
    # REBINDÉS ont été ré-enregistrés sous leur nouvelle signature à l'étape 1 (donc dans `want`)
    # et leur ancienne ligne supprimée : ils ne passent jamais par ici.
    #
    # ── ON NE DÉTRUIT PAS UN PRODUCTEUR QUE QUELQU'UN LIT ENCORE ──────────────────────────────
    # Détruire le conteneur d'un shard supprime son flux MXL. Un mur qui le mappe encore accède
    # alors à une zone démontée : le process meurt en SIGSEGV, sans trace. Le handler SIGBUS du
    # script ne couvre pas ce cas (il vise le mmap TRONQUÉ d'un producteur qui se recrée, il ne
    # s'exécute que dans le thread principal, et la faute survient ici dans un thread de calcul
    # ou d'échantillonnage). Mesuré sur le mur 333 le 2026-08-06 : 34 morts dans la journée,
    # dont 24 à moins de 30 s d'une destruction de shard, et 20 de ces 24 APRÈS elle — la
    # plupart en 0,2 à 4 s. Reconfigurer l'assembleur ne suffit pas : il referme ses Readers
    # synchrones, mais les échantillonneurs (frises, VU) se referment d'eux-mêmes, plus tard.
    # Règle : on lit ce que les murs RAPPORTENT après reconfiguration, et un shm encore listé
    # n'est pas détruit — on réessaie à la passe suivante. Même philosophie que `_asm_en_place`
    # et `_pret` : un état OBSERVÉ, pas un minuteur.
    lus, etat_inconnu = set(), False
    if etat_fn is not None:
        # `heavy` PLUS les murs qu'on vient de remettre en monolithe : ils viennent de relâcher
        # leur shard, mais leurs échantillonneurs se referment d'eux-mêmes, un peu plus tard.
        for _v in list(heavy) + sorted(_restaures):
            try:
                _e = etat_fn(_v)
            except Exception:                                              # noqa: BLE001
                _e = None
            if not isinstance(_e, dict) or "windows" not in _e:
                etat_inconnu = True     # on ignore ce que ce mur lit → on ne détruit rien ce tour
            else:
                lus.update(_norm_path(str(w.get("path") or ""))
                           for w in (_e.get("windows") or []))
    for row in db_fabric_all(node_id):
        if row["kind"] in ("shard", "shared") and row["signature"] not in want:
            _sig = row["signature"]
            if etat_fn is not None and (etat_inconnu or row["shm"] in lus):
                # ANTI-BLOCAGE (même leçon que la bascule) : une garde de sûreté ne doit pas
                # pouvoir empêcher indéfiniment le ramassage. Au-delà de _TEARDOWN_MAX passes,
                # on détruit quand même et on l'écrit au journal.
                _n = _tear_ctr.get(_sig, 0) + 1
                if _n <= _TEARDOWN_MAX:
                    _tear_ctr[_sig] = _n
                    res.setdefault("teardown_differe", []).append(_sig)
                    log.info("tissu : shard %s encore lu (ou état de mur inconnu) — destruction "
                             "reportée (%d)", _sig, _n)
                    continue
                log.warning("tissu : shard %s toujours rapporté lu après %d passes — destruction "
                            "forcée", _sig, _n - 1)
            _tear_ctr.pop(_sig, None)
            try: destroy_fn(row["ref"] or f"bobi-{name_prefix}-{_sig}")
            except Exception: pass
            db_fabric_delete(_sig); res["torn_down"].append(_sig)
    return res


def shards_par_parent():
    """{vmid du multiview parent: [vmid de ses shards]} — l'inverse de `fabric_layout`.

    Sert à rendre HONNÊTE la cadence affichée d'un mur shardé. L'assembleur recompose à sa cadence
    nominale quoi qu'il arrive : il ne dit donc RIEN de la santé de ses shards, et ceux-ci sont
    volontairement repliés dans l'interface — leur décrochage est structurellement invisible.
    Constaté le 2026-08-07 : mur affiché à 50 fps et 11 ms pendant que l'un de ses deux shards
    tournait à 44,7. Un mur en détresse se présentait comme un mur en pleine forme."""
    import json as _json
    out = {}
    for row in db_fabric_all():
        if row.get("kind") not in ("shard", "shared") or not row.get("ref"):
            continue
        try:
            parents = _json.loads(row["parents"]) if row.get("parents") else []
        except Exception:                                                  # noqa: BLE001
            parents = []
        for pv in parents:
            try:
                out.setdefault(int(pv), []).append(int(row["ref"]))
            except (TypeError, ValueError):
                continue
    return out


def fabric_layout(containers):
    """Rôle de chaque conteneur dans le tissu (pour replier les internes sous leur multiview) :
      'shard'   = nœud interne créé par le tissu (à replier sous son/ses multiview(s) parent(s)) ;
      'proxy'   = pyramide (infra de nœud PARTAGÉE entre plusieurs sources/murs → pas un seul parent) ;
      'logical' = multiview/assembleur visible, ou conteneur normal.
    `containers` = liste de dicts {vmid, deploy_config}. Renvoie {vmid: {role, parents:[vmid…]}}."""
    import json as _json
    by_ref = {}
    shard_count = {}   # {parent_vmid: nb de shards internes} → un multiview parallélisé en a ≥1
    for row in db_fabric_all():
        if row.get("kind") in ("shard", "shared") and row.get("ref"):
            try:
                parents = _json.loads(row["parents"]) if row.get("parents") else []
            except Exception:
                parents = []
            by_ref[str(row["ref"])] = parents
            for pv in parents:
                try:
                    shard_count[int(pv)] = shard_count.get(int(pv), 0) + 1
                except (TypeError, ValueError):
                    pass
    out = {}
    for c in containers:
        vmid = c.get("vmid")
        dc = c.get("deploy_config")
        try:
            dc = _json.loads(dc) if isinstance(dc, str) else (dc or {})
        except Exception:
            dc = {}
        t = (dc or {}).get("type")
        if str(vmid) in by_ref:
            out[vmid] = {"role": "shard", "parents": by_ref[str(vmid)], "shards": 0}
        elif t == "pyramide":
            out[vmid] = {"role": "proxy", "parents": [], "shards": 0}
        else:
            # 'logical' : multiview/conteneur normal. `shards` > 0 → multiview PARALLÉLISÉ
            # (c'est l'assembleur du tissu) ; 0 → autonome.
            out[vmid] = {"role": "logical", "parents": [], "shards": shard_count.get(vmid, 0)}
    return out
