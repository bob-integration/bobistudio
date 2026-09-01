# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Modèle de FLUX composables 2110 (« Option A ») pour le moteur MTL (`2110_io`).

Métadonnée orchestrateur posée PAR-DESSUS le pool de slots pré-provisionnés du moteur
(`video_count`/`audio_count`/`anc_count` côté RX ; `tx_count`/`tx_audio_count` côté TX).
Chaque flux : ``{id, essence, idx, attached_to, label}``.

- ``id``         : identité stable du flux (uuid court) — nommage/ordre/indépendance.
- ``essence``    : ``video`` | ``audio`` | ``anc``.
- ``idx``        : slot physique dans le pool de SON essence → pilote le shm
                   (``{hostname}_{idx}`` / ``_audio_{idx}`` / ``_anc_{idx}``) et le tag NMOS
                   ``urn:x-mxl:receiver_index``. Le keying NMOS reste donc vmid+idx (stable).
- ``attached_to``: ``id`` d'un flux vidéo → le flux SUIT la vidéo (câblage groupé) ; ``None`` →
                   flux INDÉPENDANT (câblé/déplacé seul).

Les compteurs ``*_count`` restent la CAPACITÉ (pool moteur) ; les listes ``rx_flows`` /
``tx_flows`` sont la vérité du GROUPEMENT et du nombre RÉEL de flux. Ce module convertit
liste↔compteurs pour que l'aval qui lit encore les compteurs (env de déploiement, budget
de queues/lcores, builder NMOS) reste cohérent sans réécriture.
"""

import uuid


# Convention de numérotation (« le 0 n'existe pas ») : la règle, ses miroirs et ses pièges sont
# documentés dans `app/numerotation.py`, qui fait foi. Ré-exportés ici parce que ce module est le
# point d'entrée naturel du modèle de flux 2110.
from .numerotation import (numero, indice, cle_tx_shm, cle_tx_audio_shm,  # noqa: F401
                           cle_tx_anc_shm, flux_video, flux_audio, flux_anc)


def _new_id():
    return uuid.uuid4().hex[:12]


def _derive_aper(n_video, n_audio, explicit=0):
    """Ratio audio/vidéo legacy (pour la MIGRATION uniquement)."""
    if explicit and explicit > 0:
        return explicit
    if n_video > 0 and n_audio % n_video == 0:
        return n_audio // n_video
    return 1 if n_audio else 0


# ── Dérivation (MIGRATION / repli) : compteurs legacy → liste de flux ─────────────────────────

def derive_rx_flows(params):
    """Construit ``rx_flows`` depuis l'état ACTIF actuel (groupement audio/ANC→vidéo par
    ``idx % n_video``, convention du builder NMOS, cf. services/nmos). Produit exactement le jeu de
    flux que NMOS rend AUJOURD'HUI (fenêtre ``active_rx_count``), pas tout le pool — la liste de
    flux = l'ensemble ACTIF, le pool ``*_count`` restant la capacité. Pour la migration des
    containers existants et le repli quand ``rx_flows`` est absent."""
    n_video_full = int(params.get("video_count") or 0)
    n_audio_full = int(params.get("audio_count") or 0)
    n_anc_full   = int(params.get("anc_count") or 0)
    arc = params.get("active_rx_count")
    n_video = min(int(arc if arc is not None else n_video_full), n_video_full)
    aper = int(params.get("audio_per_video") or 0)
    if aper > 0:
        n_audio = n_video * aper                                  # audio suit la vidéo (builder l.537)
    else:
        n_audio = min(int(arc if arc is not None else n_audio_full), n_audio_full)  # builder l.539
    n_anc = min(n_video, n_anc_full)                              # 1 ANC/vidéo, ≤ anc_count (builder l.541)
    flows, vids = [], []
    for i in range(n_video):
        fid = _new_id(); vids.append(fid)
        flows.append({"id": fid, "essence": "video", "idx": i, "attached_to": None, "label": ""})
    for i in range(n_audio):
        att = vids[i % n_video] if n_video else None
        flows.append({"id": _new_id(), "essence": "audio", "idx": i, "attached_to": att, "label": ""})
    for i in range(n_anc):
        att = vids[i % n_video] if n_video else None
        flows.append({"id": _new_id(), "essence": "anc", "idx": i, "attached_to": att, "label": ""})
    return flows


def derive_tx_flows(params):
    """Construit ``tx_flows`` depuis l'état ACTIF (``active_tx_count`` slots) + ``tx_slots[].audios``
    (groupement audio/ANC→slot vidéo). Les idx audio reprennent le schéma legacy à pas fixe
    ``i*n_aud_per_tx+ai`` pour rester alignés sur les ``tx_audio{n}_shm`` déjà persistés (le
    câblage existant ne casse pas). Comme côté RX, la liste = l'ensemble ACTIF (fenêtre
    ``active_tx_count``), pas tout le pool ``tx_count``."""
    n_tx_full = int(params.get("tx_count") or 0)
    slots = params.get("tx_slots") or []
    atc = params.get("active_tx_count")
    n_tx = min(int(atc if atc is not None else len(slots)), len(slots) or n_tx_full)
    n_aud_per_tx = max(1, (int(params.get("audio_count") or 0) // n_tx_full) if n_tx_full else 1)
    flows, vids = [], []
    # ★ VIDÉO OPTIONNELLE : un slot marqué `video_off` (sortie audio-seule / ANC-seule, cf.
    # io2110_layouts.apply_layout) n'a PAS de flux vidéo → ses audio/ANC ne s'attachent à rien
    # (attached_to=None). Un slot normal (palette) n'a pas ce marqueur → comportement historique.
    for i in range(n_tx):
        slot = slots[i] if i < len(slots) else {}
        vid = None
        if not (slot or {}).get("video_off"):
            vid = _new_id()
            flows.append({"id": vid, "essence": "video", "idx": i, "attached_to": None, "label": ""})
        vids.append(vid)
    for i in range(n_tx):
        slot = slots[i] if i < len(slots) else {}
        voff = bool((slot or {}).get("video_off"))
        # Slot vidéo : repli au pool legacy si `audios` absent. Slot sans vidéo : uniquement les audios
        # réellement déclarés (pas de repli — sinon un slot ANC-seul se verrait coller des audios).
        naud = len((slot or {}).get("audios") or []) or (0 if voff else n_aud_per_tx)
        for ai in range(naud):
            flows.append({"id": _new_id(), "essence": "audio", "idx": i * n_aud_per_tx + ai,
                          "attached_to": vids[i], "label": ""})
        # ANC : slot vidéo → historique (1 ANC/slot) ; slot sans vidéo → seulement si une dest ANC est déclarée.
        if not voff or (slot or {}).get("anc_multicast_ip"):
            flows.append({"id": _new_id(), "essence": "anc", "idx": i, "attached_to": vids[i], "label": ""})
    return flows


# ── Accès / introspection d'une liste de flux ────────────────────────────────────────────────

# ─── Niveau d'attention PAR SOURCE (présence signal) ────────────────────────────────────────
# Un gel n'est un incident que si la source est CENSÉE bouger : une mire, une ardoise ou un player
# en pause sont fixes par construction, et alerter dessus apprend à l'exploitant à ignorer son fil.
# Chaque source porte donc ses propres drapeaux. Réglage DE CONTENU par défaut permissif (décision
# 2026-07-28) : gel/noir/silence n'alertent pas tant que personne ne l'a demandé, tandis que les
# écarts de CONFORMITÉ (hors gamut, loudness) restent surveillés — ils ne dépendent pas de ce que
# la source a décidé de montrer.
# ⚠ Conséquence assumée : un vrai gel sur une caméra live ne dira rien tant que sa case n'est pas
# cochée. C'est pour ça que l'UI doit montrer l'état de chaque source, pas seulement le proposer.
# ⚠ PAS DE LOUDNESS ICI (retiré le 2026-07-28, décision utilisateur). Une mesure de loudness n'a
# de sens que RAPPORTÉE À UN PROGRAMME : elle a un début et une fin, et la conformité R128 se juge
# sur l'INTÉGRÉ de ce programme. Le moteur, lui, ne peut produire qu'un loudness MOMENTANÉ (~400 ms,
# et seulement sur L/R avec un poids 1,0 — faux dès qu'on sort du stéréo). Comparer ce momentané à
# une cible ±2 LU fait sortir n'importe quelle source vivante en permanence : un passage calme, une
# respiration, un applaudissement. C'était une alarme condamnée à battre.
# Le bon outil est un plugin DÉDIÉ, armé au début d'un programme et coupé à la fin. Le moteur
# continue de publier `lufs` (mesure brute, utile à l'affichage) — c'est l'ALARME qui disparaît.
# `clip` (saturation) : DÉFAUT ACTIF, comme le gamut. Ce n'est pas un réglage de contenu — une
# source écrêtée est en défaut quelle que soit l'intention de l'exploitant, alors qu'un silence ou
# un gel peuvent être parfaitement voulus.
# `tx_late` (2026-07-28) : trames TX en retard — le producteur câblé sur ce SLOT DE SORTIE n'a pas
# fourni l'image à temps (delta du compteur cumulatif `late` du moteur, cf. metrics.py). DÉFAUT
# ACTIF, comme clip/gamut : une trame non fournie à temps est un défaut de la chaîne quelle que
# soit l'intention de l'exploitant — il n'y a pas de « retard voulu » comme il peut y avoir un
# « gel voulu » (mire, ardoise).
ALARMES = ("frozen", "black", "silence", "clip", "gamut", "tx_late")
ALARMES_DEFAUT = {"frozen": False, "black": False, "silence": False, "clip": True, "gamut": True,
                  "tx_late": True}

# QUEL DRAPEAU S'APPLIQUE À QUEL RÔLE. `tx_late` n'a de sens QUE côté TX : c'est une mesure du
# SLOT DE SORTIE (le producteur qui alimente ce slot a-t-il livré à temps ?), rien de comparable
# n'existe côté RX. Sans cette table, l'UI afficherait une case « trames en retard » sur une source
# d'ENTRÉE où rien n'est mesuré — exactement l'erreur déjà commise (et corrigée) pour le loudness
# et l'ANC : une case affichée là où rien n'est sondé. `alarmes_par_slot` et l'API filtrent
# dessus ; l'UI (settings.html) fait de même pour la colonne.
ALARMES_ROLE = {
    "rx": ("frozen", "black", "silence", "clip", "gamut"),
    "tx": ("frozen", "black", "silence", "clip", "gamut", "tx_late"),
}

# QUOI EN FAIRE, par source. On choisit un NIVEAU, pas des canaux : le service d'alertes route déjà
# par seuil (`alerting_min_level`, surchargeable par canal — mail à partir d'« erreur », webhook à
# partir d'« avertissement »…). Cocher des destinations ici créerait une SECONDE vérité sur le
# routage, à côté de celle-là, avec la question insoluble de qui l'emporte quand elles divergent —
# et une colonne de plus à chaque canal ajouté. Le niveau, lui, compose avec l'existant et vaudra
# aussi pour les canaux futurs.
#   log     : journal technique seulement — n'entre même pas dans le fil d'alertes
#   info    : dans le fil, sous le seuil de notification par défaut → aucune notification
#   warning : comportement historique (défaut)
#   error   : remonté à tous les canaux
NIVEAUX = ("log", "info", "warning", "error")
NIVEAU_DEFAUT = "warning"


def _norm_alarmes(v):
    """Dict de drapeaux nettoyé, ou None si rien de significatif (→ défauts à la lecture)."""
    if not isinstance(v, dict):
        return None
    out = {k: bool(v[k]) for k in ALARMES if k in v}
    return out or None


def normalize(flows):
    """Nettoie/valide une liste de flux (types, champs manquants). Tolère None.

    ⚠ Cette fonction RECONSTRUIT chaque flux champ par champ : tout champ non listé ici est
    SILENCIEUSEMENT PERDU au premier passage. Ajouter un champ au modèle de flux impose donc de
    l'ajouter ICI aussi — sans quoi il disparaît sans exception ni trace, et le réglage semble
    « ne pas se sauvegarder » sans que rien ne le signale."""
    out = []
    for f in (flows or []):
        if not isinstance(f, dict):
            continue
        ess = f.get("essence")
        if ess not in ("video", "audio", "anc"):
            continue
        item = {
            "id": str(f.get("id") or _new_id()),
            "essence": ess,
            "idx": int(f.get("idx") or 0),
            "attached_to": (str(f["attached_to"]) if f.get("attached_to") else None),
            "label": str(f.get("label") or ""),
        }
        al = _norm_alarmes(f.get("alarmes"))
        if al:
            item["alarmes"] = al        # absent = défauts (on ne persiste pas du bruit)
        niv = str(f.get("niveau") or "").lower()
        if niv in NIVEAUX and niv != NIVEAU_DEFAUT:
            item["niveau"] = niv        # idem : on ne persiste que ce qui dévie du défaut
        out.append(item)
    return out


def alarmes_par_slot(dc_params, role="rx"):
    """{(essence, idx): {"drapeaux": {...}, "niveau": str}} — réglage effectif de CHAQUE slot.

    Les réglages vivent sur la source VIDÉO ; l'audio et l'ANC qui lui sont rattachés en HÉRITENT
    (`attached_to`). C'est le modèle mental de l'exploitant : « la caméra 3 », pas « le flux audio
    n°7 » — et ça évite d'avoir à configurer trois flux par source."""
    out = {}
    for f in active_flows(dc_params, role):
        # UNIQUEMENT les flux VIDÉO — et ce n'est pas un raccourci. Le moteur sonde l'image ET le son
        # d'un slot, puis publie les deux dans le signal du slot VIDÉO (`sig.update(vres)` puis
        # `sig.update(sres)` dans controller.py) ; côté orchestrateur, `_check_signal` n'est appelé
        # que pour les receivers vidéo. Le silence de `_audio_3` remonte donc sur `Rx #3`, et l'ANC
        # n'a AUCUNE sonde. Produire des entrées pour ("audio", n) / ("anc", n) donnerait des clés
        # que personne ne consulte, et une UI qui promet des réglages sans effet.
        if f.get("essence") != "video":
            continue
        niv = str(f.get("niveau") or "").lower()
        # Résolu PUIS restreint aux drapeaux du rôle (cf. ALARMES_ROLE) : un flux RX persisté avec
        # un vieux `tx_late` (bascule de rôle, éditeur bas niveau…) ne doit pas ressusciter côté UI.
        resolus = {**ALARMES_DEFAUT, **(f.get("alarmes") or {})}
        permis = ALARMES_ROLE.get(role, ALARMES)
        out[("video", f["idx"])] = {
            "drapeaux": {k: resolus[k] for k in permis},
            "niveau": niv if niv in NIVEAUX else NIVEAU_DEFAUT,
        }
    return out


def by_essence(flows, essence):
    return [f for f in (flows or []) if f.get("essence") == essence]


def video_idx_of(flows, flow):
    """idx du flux VIDÉO auquel ``flow`` est attaché, ou None si indépendant/introuvable."""
    att = flow.get("attached_to")
    if not att:
        return None
    for v in by_essence(flows, "video"):
        if v.get("id") == att:
            return v.get("idx")
    return None


def grouping_maps(flows):
    """Cartes de groupement à partir de la liste de flux, pour les consommateurs (builder NMOS,
    route RX detail). Retourne ``(video_idx_by, sub_idx_by)`` où chaque clé est ``(essence, idx)`` :
    - ``video_idx_by[(essence, idx)]`` → idx de la vidéo attachée (None = indépendant)
    - ``sub_idx_by[(essence, idx)]``   → rang du flux dans sa vidéo (0-based, pour le nommage)
    """
    flows = flows or []
    vid_of = {}   # (essence, idx) → video idx
    sub_of = {}   # (essence, idx) → rang dans la vidéo (par essence)
    counters = {}  # (video_idx|None, essence) → compteur
    for f in flows:
        if f.get("essence") == "video":
            continue
        vi = video_idx_of(flows, f)
        key = (f["essence"], f["idx"])
        vid_of[key] = vi
        ck = (vi, f["essence"])
        n = counters.get(ck, 0)
        sub_of[key] = n
        counters[ck] = n + 1
    return vid_of, sub_of


def counts(flows):
    """Compteurs dérivés (capacité MINIMALE requise) depuis la liste : nombre de flux par essence.
    Sert à garder ``*_count`` cohérents quand la liste fait foi."""
    return {
        "video": len(by_essence(flows, "video")),
        "audio": len(by_essence(flows, "audio")),
        "anc":   len(by_essence(flows, "anc")),
    }


def free_idx(flows, essence):
    """Plus petit idx libre dans le pool de ``essence`` (allocation dense, réutilise les trous)."""
    used = {f["idx"] for f in by_essence(flows, essence)}
    i = 0
    while i in used:
        i += 1
    return i


# ── Accès unifié : flux stockés (Option A) OU dérivés du legacy (repli transparent) ───────────

def tx_slot_audio_idxs(tx_flows, slot_i):
    """idx (pool plat) des flux audio TX attachés au slot vidéo ``slot_i`` (= flux vidéo dont
    ``idx == slot_i``), dans l'ordre de la liste. Pilote ``tx_audio{idx}_shm`` (entrée câblée) et
    le nombre d'entrées de ``tx_slots[i].audios`` (destinations). Liste vide si slot introuvable."""
    vid = next((f for f in (tx_flows or [])
                if f.get("essence") == "video" and f.get("idx") == slot_i), None)
    if not vid:
        return []
    return [f["idx"] for f in tx_flows
            if f.get("essence") == "audio" and f.get("attached_to") == vid.get("id")]


def tx_slot_has_anc(tx_flows, slot_i):
    """True si un flux ANC TX est attaché au slot vidéo ``slot_i``."""
    vid = next((f for f in (tx_flows or [])
                if f.get("essence") == "video" and f.get("idx") == slot_i), None)
    if not vid:
        return False
    return any(f.get("essence") == "anc" and f.get("attached_to") == vid.get("id")
               for f in tx_flows)


def active_flows(dc_params, role="rx"):
    """Liste de flux ACTIFS d'un container 2110_io. Source de vérité = ``rx_flows`` / ``tx_flows``
    si présents (modèle composable) ; sinon DÉRIVÉS des compteurs + ratio (repli pour un container
    pas encore migré / édité hors-app). Toujours triés vidéo d'abord puis par idx — l'ordre de
    rendu NMOS et UI en dépend."""
    key = "rx_flows" if role == "rx" else "tx_flows"
    flows = dc_params.get(key)
    if isinstance(flows, list) and flows:
        flows = normalize(flows)
    else:
        flows = derive_rx_flows(dc_params) if role == "rx" else derive_tx_flows(dc_params)
    order = {"video": 0, "audio": 1, "anc": 2}
    return sorted(flows, key=lambda f: (order.get(f["essence"], 9), f["idx"]))


def render_groups(flows):
    """Organise une liste de flux pour le rendu (NMOS group-hint, UI). Retourne une liste ordonnée
    de groupes ; un groupe attaché = une vidéo + ses audios/ANC ; un flux indépendant = son propre
    groupe. Chaque entrée : ``{video, audios, ancs, independent}`` où ``video`` est le flux vidéo
    (ou None pour un groupe indépendant non-vidéo), et les enfants portent ``sub`` (rang 0-based)."""
    flows = flows or []
    videos = by_essence(flows, "video")
    by_id = {v["id"]: v for v in videos}
    groups, group_of = [], {}
    for v in videos:
        g = {"video": v, "audios": [], "ancs": [], "independent": False}
        groups.append(g); group_of[v["id"]] = g
    independent = []
    for f in flows:
        if f["essence"] == "video":
            continue
        g = group_of.get(f.get("attached_to")) if f.get("attached_to") in by_id else None
        if g is None:
            independent.append(f)
        else:
            (g["audios"] if f["essence"] == "audio" else g["ancs"]).append(f)
    # rang (sub) 0-based par essence dans chaque groupe vidéo
    for g in groups:
        for i, a in enumerate(g["audios"]):
            a["sub"] = i
        for i, d in enumerate(g["ancs"]):
            d["sub"] = i
    # flux indépendants → chacun son groupe (au bout)
    for f in independent:
        groups.append({"video": None,
                       "audios": [f] if f["essence"] == "audio" else [],
                       "ancs":   [f] if f["essence"] == "anc" else [],
                       "independent": True})
    return groups
