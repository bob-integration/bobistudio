# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Classification des actions TX + fenêtre de maintenance — Étage 2 du chantier docs/reference/TX_LAYOUTS.md.

EXIGENCE PRODUIT : « une action sur un TX ne doit jamais se voir sur un autre — sauf événement de
maintenance explicitement déclaré, avertissant, et soumis à validation. »

## Le classement est CALCULÉ, pas codé en dur

Fait matériel (mesuré, cf. docs/chantiers/DPDK_NARROW.md §7 / docs/reference/TX_LAYOUTS.md) : sur le socle **narrow** (PMD ice +
pacing rate-limiter), l'arbre d'ordonnancement TX ne se modifie que par `rte_tm_hierarchy_commit`,
que le driver implémente en **arrêtant/redémarrant le PORT ENTIER** (~1 s). Côté libmtl ce commit est
déclenché par `mt_dev_get_tx_queue()` (→ `set_rl_rate` → commit TM) — c'est-à-dire par la **CRÉATION
d'une session TX**. Libérer une session (`mt_dev_put_tx_queue`) ne commit pas.

Côté moteur, le daemon `mtl_rx` réconcilie les sessions par **signature** (`mtl_rx.c:compute_sig`) :
`role|kind|mcast|port|pt|ssrc|WxH|fps|interlace|field|bd|ring|ch|ptime|epoch_shift|iface|leg2…`.
La **source** (shm d'entrée) d'un TX est explicitement HORS de la signature (patch bobi.studio) → la
changer est un simple **swap** (`tx_set_source`). Une signature qui change = ancienne session libérée
+ **nouvelle session créée** = commit = blip du port.

D'où la règle unique de ce module :

    ACTION PERTURBATRICE  ⟺  elle fait APPARAÎTRE une signature de session TX **VIDÉO** qui
                             n'existait pas, SUR UN PORT EN RATE-LIMITER (pmd=dpdk + pacing rl).

Pourquoi « vidéo » seulement : libmtl ne pose une feuille RL (`set_rl_rate` → commit) que si le débit
demandé est non nul — `if (inf->tx_pacing_way == RL && bytes_per_sec)` (mt_dev.c:1551). L'audio (st30)
et l'ANC (st40) demandent un débit 0 : leur file est obtenue SANS set_rl_rate, donc SANS commit.
VÉRIFIÉ AU BANC (moteur 140, nœud 30) : câbler une sortie provisionnée crée ses sessions audio + ANC
et ne produit AUCUN commit. Tout le reste est SÛR.

Sur un port **AF-XDP** (pas de RL matériel), il n'y a **aucun commit** possible : tout y est sûr, et
l'UI doit le DIRE au lieu de rester muette (cf. `port_mode`). On ne devine pas les signatures : on les dérive des
payloads `:8081/tx` RÉELLEMENT poussés (`docker_driver.tx_payloads`, source unique de vérité) en
rejouant les règles d'émission du contrôleur (`controller.py`, boucle `_emit_tx`).

## Fenêtre de maintenance

Une action perturbatrice peut être **différée** : elle s'empile dans un bac (table
`tx_pending_changes`, persistante) et tout est appliqué **ensemble** — un seul `push_tx_slots`, donc
un seul passage de réconciliation du daemon. Application immédiate, ou planifiée à HH:MM (thread
`start_scheduler`, lancé depuis `main.py`).

⚠ Ce que le groupage économise VRAIMENT (mesuré au banc, à ne pas survendre) : le commit TM est fait
**par session recréée**, pas par action. Grouper 3 actions qui touchent 3 sorties DIFFÉRENTES coûte
toujours 3 recréations ; grouper 3 actions sur LA MÊME sortie (format + TROFF + destination) ne coûte
qu'**UNE** recréation au lieu de 3. Le vrai bénéfice opérationnel est donc double : (1) fusion des
changements d'une même sortie, et (2) **une seule fenêtre**, choisie et annoncée, au lieu de N
perturbations dispersées pendant l'antenne.
"""

import json
from .numerotation import cle_tx_shm, cle_tx_audio_shm, cle_tx_anc_shm
import logging
import threading
import time

log = logging.getLogger(__name__)

# Opérations différables (mutations pures de `params`, appliquées en lot puis poussées une fois).
DEFERRABLE = ("tx_dest", "tx_format", "tx_pacing", "tx_serve_newest", "tx_layout_apply",
               "tx_mcast_plan")

# ─── Axes de format qui font RÉELLEMENT changer la signature (étage 3) ─────────────────────────────
# Établis en LISANT le code, pas en raisonnant : `mtl_rx.c:compute_sig` (identité de session) inclut
#   role|kind|mcast|port|pt|ssrc|WxH|fps|interlaced|tff|bit_depth|ring|channels|ptime|epoch_shift|iface…
# et le câblage POUSSE le format de la source dans le slot (`controller.py:/input` → t[w|h|bd|fps|
# scan|field_order]). L'intersection des deux = les 6 axes ci-dessous. Tout le reste (CHROMA,
# colorimétrie, pix_fmt) n'entre NI dans la signature NI dans `/input` → un écart y est INOFFENSIF
# pour l'arbre TX : on ne bloque pas dessus (on avertit pour le chroma, qui casse l'image sans
# recréer la session — la CHROMA du moteur est une constante d'image, pas une clé de session).
SIG_FORMAT_AXES = ("width", "height", "fps", "scan", "field_order", "bit_depth")


def _norm_fps(fps, scan):
    """Cadence TRAME (celle qui entre dans la signature — cf. `_video_format`). Une source entrelacée
    déclare souvent sa cadence CHAMP (1 grain MXL = 1 champ) : 1080i50 → 25 trames/s."""
    try:
        f = float(fps or 0)
    except (TypeError, ValueError):
        return 0.0
    if str(scan or "p") == "i" and f > 30:
        f /= 2.0
    return round(f, 3)


def slot_format(params, slot):
    """Format DÉCLARÉ d'un slot TX (celui qu'annonce le SDP quand la sortie ne suit pas de source),
    normalisé sur les 6 axes de signature. `bit_depth` : 8 par défaut — c'est la valeur réellement
    poussée au contrôleur (`docker_driver.tx_payloads` envoie `int(t.get('bit_depth') or 8)`)."""
    slots = (params or {}).get("tx_slots") or []
    try:
        t = dict(slots[int(slot)] or {})
    except (IndexError, TypeError, ValueError):
        return None
    scan = "i" if str(t.get("scan") or "p").lower() == "i" else "p"
    return {"width": int(t.get("width") or 0), "height": int(t.get("height") or 0),
            "fps": _norm_fps(t.get("fps"), scan), "scan": scan,
            "field_order": (str(t.get("field_order") or "").lower() if scan == "i" else ""),
            "bit_depth": int(t.get("bit_depth") or 8)}


def format_diff(src_fmt, slot_fmt):
    """Écarts de format ENTRE une source et un slot TX, sur les seuls axes qui changent la signature.
    Retourne [{axis, source, slot}] — vide si les formats concordent (⇒ câblage = swap, zéro commit).
    Un axe inconnu d'un côté (0/vide) n'est PAS un écart : on ne crie pas sur ce qu'on ne sait pas."""
    if not src_fmt or not slot_fmt:
        return []
    s = dict(src_fmt)
    s_scan = "i" if str(s.get("scan") or "p").lower() == "i" else "p"
    s = {"width": int(s.get("width") or 0), "height": int(s.get("height") or 0),
         "fps": _norm_fps(s.get("fps") or s.get("fps_num"), s_scan), "scan": s_scan,
         "field_order": (str(s.get("field_order") or "").lower() if s_scan == "i" else ""),
         "bit_depth": int(s.get("bit_depth") or 0)}
    out = []
    for ax in SIG_FORMAT_AXES:
        a, b = s.get(ax), slot_fmt.get(ax)
        if ax == "field_order":
            # L'ordre de champ ne compte QUE si les deux sont entrelacés (en progressif il vaut "").
            if s["scan"] != "i" or slot_fmt.get("scan") != "i" or not a or not b:
                continue
        elif ax == "bit_depth":
            if not a or not b:
                continue
        elif not a or not b:
            continue
        if str(a) != str(b):
            out.append({"axis": ax, "source": a, "slot": b})
    return out


# ─── Mode d'un port : le RL (donc le risque de blip) n'existe qu'en DPDK ──────────────────────────

def port_mode(node, iface):
    """Mode d'émission d'un port média : dict {pmd, rl, why}. `rl=True` ⇒ pacing par rate-limiter
    matériel ⇒ toute création de session TX recale l'arbre (commit TM = stop/start du port). En
    af_xdp/kernel le pacing est TSC (logiciel) : aucun arbre, aucun commit — TOUT est sûr."""
    from .database import db_get_node_interfaces
    from . import docker_driver as _dd
    row = {}
    for r in (db_get_node_interfaces((node or {}).get("id")) or []):
        if r.get("ifname") == iface:
            row = r
            break
    pmd = (row.get("pmd") or "").strip().lower() or "af_xdp"
    if pmd not in ("dpdk", "sriov"):
        return {"pmd": pmd, "rl": False, "why": "no_dpdk"}
    try:
        pacing, _ = _dd._derive_pacing(node)
    except Exception:
        pacing = None
    # `_derive_pacing` → None si aucun profil posé ; le contrôleur retombe alors sur MTL_PACING=auto
    # = RL sur port E810 dpdk → traiter None comme 'rl' (même convention que _mtl_rl_tx_budget).
    rl = (pacing or "rl") == "rl"
    return {"pmd": pmd, "rl": rl, "why": "rl" if rl else "tsc"}


def _primary_iface(node):
    from . import docker_driver as _dd
    mifs = _dd._media_ifaces(node) or []
    return mifs[0].get("ifname") if mifs else None


class _PortModes:
    """Mémo des modes de port pour UN calcul de verdict. `port_mode`/`_primary_iface` font chacun une
    requête SQL ; sans mémo ils étaient appelés par SESSION (× 25 slots × N moteurs, toutes les 3 s
    par le poll de Destinations)."""

    def __init__(self, node):
        self.node = node
        self._cache = {}
        self.primary = _primary_iface(node)

    def rl(self, iface):
        if iface not in self._cache:
            self._cache[iface] = port_mode(self.node, iface)
        return bool(self._cache[iface].get("rl"))

    def mode(self, iface):
        if iface not in self._cache:
            self._cache[iface] = port_mode(self.node, iface)
        return self._cache[iface]


# ─── Signatures de sessions TX (miroir de controller._emit_tx + mtl_rx.compute_sig) ───────────────

def _video_format(ent, slot_decl):
    """Format EFFECTIF de la session vidéo d'un slot TX — celui qui entre dans la signature mtl_rx.
    Deux gouvernances (miroir exact de `docker_driver.tx_payloads` + `controller.py :8082/input`) :
      · slot en GÉN (mire, ou pas de câble) → le format DÉCLARÉ du slot gouverne (payload non nul) ;
      · slot CÂBLÉ qui suit sa source      → la SOURCE gouverne (le push envoie 0, et `/input` pose
        w/h/bd/fps/scan de la source dans l'état du slot).
    ⚠ MESURÉ AU BANC : c'est de là que vient le seul piège de l'étage 1 — câbler une source dont le
    format DIFFÈRE du format provisionné du slot change la signature ⇒ session RECRÉÉE ⇒ commit TM.
    Le swap gratuit n'existe que si les formats CONCORDENT (d'où l'étage 3 : gate de format + UDC)."""
    p = ent["payload"]
    if p.get("width"):                      # gen : le slot déclare son format
        w, h = p.get("width"), p.get("height")
        fps, bd = p.get("fps"), p.get("bit_depth")
        scan, fo = p.get("scan"), p.get("field_order")
    else:                                   # câblé : la source gouverne (repli = format du slot)
        src = ent.get("src") or {}
        t = slot_decl or {}
        w   = src.get("w") or t.get("width")
        h   = src.get("h") or t.get("height")
        fps = src.get("fps") or t.get("fps")
        bd  = src.get("bit_depth") or t.get("bit_depth")
        scan = src.get("scan") or t.get("scan") or "p"
        fo   = src.get("field_order") or t.get("field_order") or ""
    try:
        fps = float(fps or 0)
    except (TypeError, ValueError):
        fps = 0.0
    if str(scan) == "i" and fps > 30:        # ST 2110-20 : cadence TRAME (mtl_rx._tx_session)
        fps /= 2.0
    return w, h, fps, bd, scan, fo


def _slot_sessions(ent, node, params, primary=None):
    """Sessions TX qu'un payload `:8081/tx` fera exister côté contrôleur, avec leur SIGNATURE.
    Rejoue EXACTEMENT les gardes d'émission de `controller.py` (boucle `_emit_tx`) :

      · vidéo : `mcast && udp_port && ((enabled && shm_in) || provisioned)`
      · audio : `enabled && (tonalité || (mire && pas de câble vidéo) || câble audio)` + dest valide
      · ANC   : `enabled && anc dest valide && câble ANC`

    Seule la VIDÉO est pré-provisionnée par l'étage 1 : câbler une sortie crée aussi ses sessions
    AUDIO/ANC. C'est SANS conséquence (elles ne demandent aucun débit → aucune feuille RL → aucun
    commit, cf. `classify`) — mesuré au banc. On les modélise quand même : elles occupent des files."""
    p = ent["payload"]
    i = ent["i"]
    iface = (p.get("iface") or "").strip() or primary or _primary_iface(node) or "auto"
    slot_decl = ((params or {}).get("tx_slots") or [{}] * (i + 1))[i] if i < len(
        (params or {}).get("tx_slots") or []) else {}
    out = []
    enabled = bool(p.get("enabled"))            # = un shm vidéo est câblé sur ce slot
    prov    = bool(p.get("provisioned"))
    mc, up  = p.get("mcast"), int(p.get("udp_port") or 0)
    if mc and up and (enabled or prov):
        w, h, fps, bd, scan, fo = _video_format(ent, slot_decl)
        out.append({
            "iface": iface, "essence": "video", "slot": i,
            # `sn` (serve_newest) fait partie de la signature mtl_rx : l'omettre ici ferait
            # conclure « aucune session ne change » à une action qui les recrée pourtant.
            "sig": "tx|v|%s|%s:%d|%s|%sx%s|%s|%s|%s|bd%s|r%s|es%s|sn%s|%s:%s" % (
                iface, mc, up, p.get("pt"), w, h, fps, scan, fo, bd, p.get("ring"),
                p.get("epoch_shift_us"), p.get("serve_newest"),
                p.get("mcast2"), p.get("udp_port2")),
            # Une session vidéo PROVISIONNÉE sans source est SILENCIEUSE (feuille RL créée, 0 Gb/s).
            "silent": not enabled,
        })
    for ai, a in enumerate(p.get("audios") or []):
        amc, aup = a.get("mcast"), int(a.get("port") or 0)
        if not (amc and aup and enabled):
            continue
        _tone = bool((a.get("tone") or {}).get("enabled"))
        _gen  = bool(p.get("gen_enabled"))
        _cab  = (p.get("audio_shm_in") or [])
        _has_src = _tone or _gen or (ai < len(_cab) and bool(_cab[ai]))
        if not _has_src:
            continue
        out.append({"iface": iface, "essence": "audio", "slot": i, "audio_idx": ai, "silent": False,
                    "sig": "tx|a|%s|%s:%d|%s|%s:%s" % (iface, amc, aup, a.get("pt"),
                                                       a.get("mcast2"), a.get("port2"))})
    dmc, dup = p.get("anc_mcast"), int(p.get("anc_port") or 0)
    if enabled and dmc and dup and (p.get("anc_shm_in") or ""):
        out.append({"iface": iface, "essence": "anc", "slot": i, "silent": False,
                    "sig": "tx|d|%s|%s:%d|%s|%s|%s:%s" % (iface, dmc, dup, p.get("anc_pt"),
                                                          p.get("fps"), p.get("anc_mcast2"),
                                                          p.get("anc_port2"))})
    return out


def tx_sessions(vmid, params, node=None, primary=None):
    """Toutes les sessions TX (avec signature + port) que le moteur `vmid` fera exister avec `params`."""
    from .database import db_get_container, db_get_node
    from . import docker_driver as _dd
    if node is None:
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id")) or {}
    if primary is None:
        primary = _primary_iface(node)
    out = []
    try:
        ents = _dd.tx_payloads(vmid, params) or []
    except Exception as e:                      # jamais bloquer une action sur un défaut de calcul
        log.warning("tx_sessions %s: %s", vmid, e)
        return []
    # Le contrôleur ne crée de session que pour les slots < ACTIVE_TX_COUNT (budget bootté).
    act = int((params or {}).get("active_tx_count") or 0) or len(ents)
    for ent in ents:
        if ent["i"] >= act:
            continue
        out.extend(_slot_sessions(ent, node, params, primary))
    return out


def _slot_label(params, slot):
    """Nom lisible d'une sortie (pour NOMMER les victimes) : label du flux TX, sinon « TX #n »."""
    try:
        from . import io2110_flows as _iof
        for f in _iof.active_flows(params or {}, "tx"):
            if f.get("essence") == "video" and int(f.get("idx") or 0) == int(slot):
                if (f.get("label") or "").strip():
                    return f["label"].strip()
    except Exception:
        pass
    return "TX #%d" % (int(slot) + 1)


# ─── Classification d'une action ──────────────────────────────────────────────────────────────────

def classify(vmid, params_after, op="tx_edit", params_before=None):
    """Verdict d'une action sur un moteur 2110_io. Retourne un dict :

        {level: 'safe'|'disruptive', reason, ports:[iface…], victims:[{slot,label,essence}],
         victim_count, created:[…], rl: bool, port_mode:{…}, deferrable: bool, engine, node}

    `op` :
      · 'deploy' | 'restart' | 'realign' | 'recreate' → perturbateur par construction (mtl_init).
      · toute autre valeur → verdict CALCULÉ : diff des signatures de sessions TX (avant → après).
    Un port qui n'est pas en RL (af_xdp) ne peut PAS blipper → l'action y est sûre quoi qu'elle fasse.
    """
    import json as _json
    from .database import db_get_container, db_get_node
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id")) or {}
    if params_before is None:
        try:
            params_before = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:
            params_before = {}
    pmc = _PortModes(node)
    prim = pmc.primary
    pm = pmc.mode(prim) if prim else {"pmd": "?", "rl": False, "why": "no_iface"}
    base = {"engine": c.get("hostname") or ("#%s" % vmid), "vmid": vmid,
            "node": node.get("name") or node.get("host") or "", "node_id": node.get("id"),
            "iface": prim, "port_mode": pm, "rl": bool(pm.get("rl")),
            "victims": [], "victim_count": 0, "ports": [], "created": [],
            "deferrable": op in DEFERRABLE}

    before = tx_sessions(vmid, params_before, node, prim)
    # Victimes potentielles = les sorties qui ÉMETTENT réellement (session vivante avec une source).
    def _victims(ifaces):
        """Les victimes sont des SORTIES (un slot = une sortie nommée dans l'UI), pas des sessions :
        une sortie qui émet vidéo + audio + ANC ne doit être NOMMÉE et COMPTÉE qu'UNE fois."""
        by_slot = {}
        for s in before:
            if s["iface"] not in ifaces or s.get("silent"):
                continue
            v = by_slot.setdefault(s["slot"], {
                "slot": s["slot"], "label": _slot_label(params_before, s["slot"]), "essences": []})
            if s["essence"] not in v["essences"]:
                v["essences"].append(s["essence"])
        return [by_slot[k] for k in sorted(by_slot)]

    if op in ("deploy", "restart", "realign", "recreate"):
        ifaces = {s["iface"] for s in before} or ({prim} if prim else set())
        _vic = _victims(ifaces)
        return dict(base, level="disruptive", scope="engine",
                    reason="engine_restart", ports=sorted(ifaces),
                    victims=_vic, victim_count=len(_vic))

    after = tx_sessions(vmid, params_after, node, prim)
    sig_before = {s["sig"] for s in before}
    created = [s for s in after if s["sig"] not in sig_before]
    # Seules les sessions VIDÉO sont mises en forme par le rate limiter. libmtl ne pose une feuille RL
    # (`dev_tx_queue_set_rl_rate` → `rte_tm_hierarchy_commit` → stop/start du port) que si le débit
    # demandé est NON NUL : `if (inf->tx_pacing_way == RL && bytes_per_sec)` (mt_dev.c:1551). L'audio
    # (st30) et l'ANC (st40) demandent un débit 0 → file obtenue SANS set_rl_rate → AUCUN commit.
    # VÉRIFIÉ AU BANC (moteur 140) : câbler une sortie provisionnée crée ses sessions audio + ANC et
    # ne produit ZÉRO `mt_dev_get_tx_queue` avec débit → zéro blip.
    # Un port sans RL (af_xdp : pacing TSC logiciel) ne commit jamais non plus.
    rl_created = [s for s in created if s["essence"] == "video" and pmc.rl(s["iface"])]
    if not rl_created:
        return dict(base, level="safe", scope="slot",
                    reason=("no_new_session" if not created else "no_rl_leaf"),
                    # Sessions créées mais SANS feuille RL (audio/ANC, ou port non-RL) → gratuites.
                    created_free=[{"slot": s["slot"], "essence": s["essence"]} for s in created])
    ifaces = sorted({s["iface"] for s in rl_created})
    vic = _victims(set(ifaces))
    return dict(base, level="disruptive", scope="port", reason="tm_commit", ports=ifaces,
                created=[{"slot": s["slot"], "essence": s["essence"], "iface": s["iface"]}
                         for s in rl_created],
                victims=vic, victim_count=len(vic))


# ─── Mutations de params (rejouées telles quelles à l'application différée) ────────────────────────

def _mut_tx_dest(params, a):
    slots = list(params.get("tx_slots") or [])
    i = int(a["slot"])
    if not (0 <= i < len(slots)):
        raise ValueError("slot TX #%d inexistant" % i)
    slots[i] = dict(slots[i] or {})
    essence = a.get("essence") or "video"
    leg = 1 if int(a.get("leg") or 0) == 1 else 0
    sfx = "_leg1" if leg else ""
    mcast, port = a["mcast"], int(a["port"])
    if essence == "audio":
        ai = max(0, int(a.get("audio_idx") or 0))
        audios = [dict(x or {}) for x in (slots[i].get("audios") or [])]
        while len(audios) <= ai:
            audios.append({})
        audios[ai]["multicast_ip" + sfx] = mcast
        audios[ai]["dest_port" + sfx] = port
        slots[i]["audios"] = audios
    else:
        pfx = "" if essence == "video" else essence + "_"
        slots[i]["%smulticast_ip%s" % (pfx, sfx)] = mcast
        slots[i]["%sdest_port%s" % (pfx, sfx)] = port
    params["tx_slots"] = slots
    return params


def _mut_tx_format(params, a):
    slots = list(params.get("tx_slots") or [])
    i = int(a["slot"])
    if not (0 <= i < len(slots)):
        raise ValueError("slot TX #%d inexistant" % i)
    slots[i] = dict(slots[i] or {})
    fps = float(a["fps"])
    scan = "i" if str(a.get("scan") or "p").lower() == "i" else "p"
    if scan == "i" and fps > 30:                  # ST 2110-20 : cadence TRAME, jamais champ
        fps /= 2.0
    slots[i]["width"], slots[i]["height"] = int(a["width"]), int(a["height"])
    slots[i]["fps"], slots[i]["scan"] = fps, scan
    if scan == "i":
        fo = str(a.get("field_order") or "").lower()
        if fo in ("tff", "bff"):
            slots[i]["field_order"] = fo
        else:
            slots[i].setdefault("field_order", "tff")
    else:
        slots[i]["field_order"] = ""
    # Profondeur (étage 3, « aligner le slot sur la source ») : elle entre dans la signature ET est
    # poussée à chaque `/tx` — l'omettre laisserait un écart de bd derrière un alignement « réussi ».
    if a.get("bit_depth"):
        slots[i]["bit_depth"] = int(a["bit_depth"])
    params["tx_slots"] = slots
    return params


def _mut_tx_pacing(params, a):
    slots = list(params.get("tx_slots") or [])
    i = int(a["slot"])
    if not (0 <= i < len(slots)):
        raise ValueError("slot TX #%d inexistant" % i)
    slots[i] = dict(slots[i] or {})
    slots[i]["epoch_shift_us"] = max(0, min(15000, int(a.get("epoch_shift_us") or 0)))
    params["tx_slots"] = slots
    return params


def _mut_tx_wire(params, a):
    """Câblage d'une sortie TX (miroir de `cabling._apply_wire` : le shm est persisté dans le
    `state_field` du slot). PAS différable (le câble est un geste atomique côté page Câbles) mais
    CLASSIFIABLE : câbler une source dont le format concorde avec le slot provisionné = swap de source
    (0 commit, PROUVÉ AU BANC) ; une source de format différent recrée la session (commit)."""
    p = dict(params)
    slot = int(a["slot"])
    shm = (a.get("shm") or "").strip()
    kind = (a.get("kind") or "video").lower()
    if kind == "audio":
        p[cle_tx_audio_shm(int)(a.get("audio_slot") or slot)] = shm
    elif kind in ("anc", "data"):
        p[cle_tx_anc_shm(slot)] = shm
    else:
        p[cle_tx_shm(slot)] = shm
    return p


def _mut_tx_serve_newest(params, a):
    """Choix de la trame émise (1 = la plus récemment prête, défaut ; 0 = la plus ancienne).
    Même classe que `tx_pacing` : le champ est DANS la signature de session mtl_rx, donc le
    changer recrée la session — d'où le passage par le garde-fou et la différabilité."""
    slots = list(params.get("tx_slots") or [])
    i = int(a["slot"])
    if not (0 <= i < len(slots)):
        raise ValueError("slot TX #%d inexistant" % i)
    slots[i] = dict(slots[i] or {})
    slots[i]["serve_newest"] = 1 if int(a.get("serve_newest") or 0) else 0
    params["tx_slots"] = slots
    return params


MUTATORS = {"tx_dest": _mut_tx_dest, "tx_format": _mut_tx_format, "tx_pacing": _mut_tx_pacing,
            "tx_serve_newest": _mut_tx_serve_newest, "tx_wire": _mut_tx_wire}


def mutate(params, op, args):
    """Applique une action différable à `params` (copie) et retourne les params résultants."""
    if op in ("tx_layout_apply", "tx_mcast_plan"):
        # Cas à part : ces deux actions RECALCULENT elles-mêmes les params (io2110_layouts.apply_layout
        # / allocations.plan_tx_multicast) et se persistent/poussent seules à l'application.
        return params
    fn = MUTATORS.get(op)
    if not fn:
        raise ValueError("opération non différable : %s" % op)
    return fn(dict(params), args or {})


def preview(vmid, op, args):
    """Params RÉSULTANTS d'une action (sans rien persister) — sert au verdict de pré-vol."""
    import json as _json
    from .database import db_get_container
    c = db_get_container(vmid) or {}
    try:
        params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        params = {}
    if op == "tx_layout_apply":
        from . import io2110_layouts as _lay
        return _lay.preview_layout_params(vmid, params)
    if op == "tx_mcast_plan":
        from . import allocations as _alloc
        return _alloc.preview_plan_params(vmid, params)
    return mutate(params, op, args)


def action_label(op, args):
    a = args or {}
    slot = ("TX #%d" % (int(a["slot"]) + 1)) if a.get("slot") is not None else ""
    if op == "tx_format":
        return "%s → %sx%s%s%s" % (slot, a.get("width"), a.get("height"),
                                   a.get("scan") or "p", _norm_fps(a.get("fps"), a.get("scan")))
    if op == "tx_dest":
        return "%s %s → %s:%s" % (slot, (a.get("essence") or "video"), a.get("mcast"), a.get("port"))
    if op == "tx_pacing":
        return "%s TROFF %s µs" % (slot, a.get("epoch_shift_us") or 0)
    if op == "tx_serve_newest":
        return "%s trame %s" % (slot, "la plus récente" if a.get("serve_newest")
                                else "la plus ancienne")
    if op == "tx_layout_apply":
        return "layout TX"
    if op == "tx_mcast_plan":
        return "plan multicast"
    if op == "tx_wire":
        return "%s ← %s" % (slot, a.get("shm") or "—")
    return op


# ─── Bac de changements en attente (fenêtre de maintenance) ────────────────────────────────────────

def queue(vmid, op, args, apply_at=None, actor=None):
    """Empile une action perturbatrice dans le bac. `apply_at` = ISO 'YYYY-MM-DDTHH:MM' (ou None =
    application manuelle). Rien n'est poussé au moteur ici : aucun blip tant qu'on n'applique pas."""
    from .database import db_get_container, db_tx_pending_add
    if op not in DEFERRABLE:
        return None, "opération non différable (%s)" % op
    c = db_get_container(vmid) or {}
    if not c:
        return None, "moteur introuvable"
    verdict = classify(vmid, preview(vmid, op, args), op=op)
    pid = db_tx_pending_add(vmid=vmid, node_id=c.get("node_id"),
                            iface=(verdict.get("ports") or [None])[0] or verdict.get("iface"),
                            op=op, args=args or {}, label=action_label(op, args),
                            apply_at=apply_at, created_by=actor or "")
    return pid, None


def list_pending(vmid=None):
    from .database import db_tx_pending_list
    return db_tx_pending_list(vmid=vmid, status="pending")


def cancel(pid):
    """Annule un changement en attente. Un `tx_dest` différé a RÉSERVÉ son adresse multicast dès la
    mise au bac (pour qu'un autre flux ne la souffle pas d'ici l'application) → l'annulation doit la
    RENDRE, sinon l'adresse reste marquée occupée par un changement qui n'aura jamais lieu."""
    from .database import db_tx_pending_get, db_tx_pending_set_status, db_release_mcast_owner
    p = db_tx_pending_get(pid)
    if p and p.get("op") == "tx_dest":
        a = p.get("args") or {}
        try:
            ref = "tx:%s:%s:%s:override:%s:leg%s" % (
                p["vmid"], int(a.get("slot")), a.get("essence") or "video",
                int(a.get("audio_idx") or 0), 1 if int(a.get("leg") or 0) == 1 else 0)
            db_release_mcast_owner(ref)
        except Exception as e:
            log.warning("cancel %s: libération mcast: %s", pid, e)
    db_tx_pending_set_status(pid, "cancelled")


def schedule(vmid, apply_at):
    """(Re)planifie TOUT le bac d'un moteur à une heure donnée (ISO 'YYYY-MM-DDTHH:MM')."""
    from .database import db_tx_pending_set_apply_at
    n = 0
    for p in list_pending(vmid):
        db_tx_pending_set_apply_at(p["id"], apply_at)
        n += 1
    return n


def apply_pending(vmid, actor=""):
    """Applique TOUT le bac d'un moteur EN UN SEUL LOT : on rejoue chaque mutation sur les params
    courants, on persiste UNE fois, on pousse UNE fois (`push_tx_slots`) → les N créations de sessions
    tombent dans le MÊME commit TM = **un seul blip** au lieu de N. Retourne (n_appliqués, erreurs)."""
    import json as _json
    from .database import (db_get_container, db_update_deploy_config, db_add_alert,
                           db_tx_pending_set_status)
    from . import docker_driver, io2110_layouts as _lay
    from .vmlocks import verrou_vmid

    pend = list_pending(vmid)
    if not pend:
        return 0, []
    errors, applied, layout_apply, mcast_plan = [], [], False, False
    layout_iface = None      # carte ciblée mémorisée par l'op différée (portée multi-port)
    with verrou_vmid(vmid, op="tx-maintenance"):
        c = db_get_container(vmid) or {}
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            dc = {}
        params = dict(dc.get("params") or {})
        for p in pend:
            try:
                if p["op"] == "tx_layout_apply":
                    layout_apply = True                 # appliqué en dernier (alloc + provisioning)
                    # Portée MÉMORISÉE : différer ne doit pas élargir l'application de la carte
                    # cliquée à toutes les cartes du nœud (multi-port).
                    layout_iface = (p.get("args") or {}).get("iface") or layout_iface
                elif p["op"] == "tx_mcast_plan":
                    mcast_plan = True                   # idem : replanifie + persiste + pousse seul
                else:
                    params = mutate(params, p["op"], p["args"])
                applied.append(p)
            except Exception as e:
                errors.append("#%s %s : %s" % (p["id"], p["label"], e))
                db_tx_pending_set_status(p["id"], "failed", str(e))
        if applied:
            db_update_deploy_config(vmid, dc.get("type") or "2110_io", params)
            if layout_apply:
                # apply_layout persiste + pousse lui-même (alloc mcast + provisioning silencieux).
                ok, res = _lay.apply_layout(vmid, iface=layout_iface)
                if not ok:
                    errors.append("layout : %s" % res)
                if mcast_plan:                          # replan APRÈS le layout (adresses fraîches)
                    from . import allocations as _alloc
                    _d, _e = _alloc.plan_tx_multicast(vmid, appliquer=True)
                    if _e:
                        errors.append("plan multicast : %s" % _e)
            elif mcast_plan:
                from . import allocations as _alloc
                _d, _e = _alloc.plan_tx_multicast(vmid, appliquer=True)
                if _e:
                    errors.append("plan multicast : %s" % _e)
            else:
                try:
                    docker_driver.push_tx_slots(vmid, params)
                except Exception as e:
                    errors.append("push : %s" % e)
            for p in applied:
                db_tx_pending_set_status(p["id"], "applied")
    if applied:
        _labels = ", ".join(p["label"] for p in applied)
        if errors:
            db_add_alert("alert.tx_stall.maintenance_appliquee_avec_erreurs", "warning",
                        vmid=vmid, kind="tx_stall",
                        params={"h": c.get("hostname") or vmid, "n": len(applied),
                                "labels": _labels, "errors": "; ".join(errors)})
        else:
            db_add_alert("alert.tx_stall.maintenance_appliquee", "info",
                        vmid=vmid, kind="tx_stall",
                        params={"h": c.get("hostname") or vmid, "n": len(applied),
                                "labels": _labels})
    try:
        from services import nmos as _nmos
        _nmos.notify_state_change()
    except Exception:
        pass
    return len(applied), errors


# ─── Planificateur (« appliquer à HH:MM ») ────────────────────────────────────────────────────────

_sched_started = False


def start_scheduler(interval=20):
    """Thread daemon : applique les lots dont l'heure est venue. Idempotent (un seul thread)."""
    global _sched_started
    if _sched_started:
        return
    _sched_started = True

    def _loop():
        while True:
            try:
                from .database import db_tx_pending_due
                due = db_tx_pending_due(time.strftime("%Y-%m-%dT%H:%M"))
                for vmid in sorted({p["vmid"] for p in due}):
                    log.info("fenêtre de maintenance TX : application planifiée du bac de %s", vmid)
                    apply_pending(vmid, actor="scheduler")
            except Exception as e:
                log.warning("tx_maintenance scheduler: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="tx-maintenance").start()
