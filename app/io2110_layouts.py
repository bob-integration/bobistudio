# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Layouts TX déclarés par carte (NIC média) — Étage 1 du chantier docs/reference/TX_LAYOUTS.md.

Contexte : sur le socle narrow, chaque CRÉATION de session TX (`rte_tm_hierarchy_commit`) stoppe/
redémarre le port entier et peut tuer silencieusement les sessions déjà vivantes (cf. docs/reference/TX_LAYOUTS.md,
docs/chantiers/DPDK_NARROW.md §7). L'étage 1 déplace ces créations au (re)DÉPLOIEMENT du moteur, quand aucune sortie
n'est encore vivante : on déclare un LAYOUT (composition libre de slots TX — format vidéo + nombre de
flux audio + ANC oui/non) par NIC média, on l'« applique » à un moteur `2110_io` déployé sur cette NIC
(auto-alloc mcast/port + écriture de `tx_slots[i]` + flag `provisioned=True` poussé au contrôleur), et
ensuite le câblage/l'activation d'une sortie n'est plus qu'un SWAP DE SOURCE (zéro commit) — le flag
`provisioned` et le découplage source↔session existent déjà côté `plugins/2110_io/docker/controller.py`
(jamais posés jusqu'ici, cf. push_tx_slots dans `docker_driver.py`).

Persistance : un blob JSON par (node_id, iface) dans la table `settings` générique (clé synthétique
`tx_layout_<node_id>_<iface>`, via `db_get_setting`/`db_set_setting` — aucune migration de schéma).
Le layout vit dans Réglages (édition ADMIN — permission `settings.edit`) ; la page Destinations 2110
l'affiche en lecture seule + bouton « Appliquer » (permission `containers.deploy`, cf. docs/reference/TX_LAYOUTS.md
décision #1).

Ce module ne fait AUCUN appel réseau lui-même à part via `docker_driver.push_tx_slots` (à l'apply) et
`allocations.allocate_multicast_for` (réservation d'adresses) — pas de code plugin exécuté in-process.
"""

import logging
from .numerotation import cle_tx_shm, cle_tx_audio_shm, cle_tx_anc_shm
import time

log = logging.getLogger(__name__)


# ─── Bibliothèque de PRESETS suggérés (NON contraignants, cf. docs/reference/TX_LAYOUTS.md décision #3) ──────────
# Clé = sous-chaîne de modèle NIC (lower), comme app.mtl.NIC_RL_TX_CAP. "_default" = repli générique.
# Chaque preset = liste de slots {video:{w,h,fps,bd,scan}, audio_count, anc}.
def _fmt(w, h, fps, bd=10, scan="p"):
    return {"w": w, "h": h, "fps": fps, "bd": bd, "scan": scan}


PRESETS = {
    # E810-C mesuré (63 sessions TX narrow/port, cf. app/mtl.py NIC_RL_TX_CAP) : quelques
    # compositions repères, à ajuster (compositions LIBRES, cf. décision produit #3).
    "e810-c": [
        {"label": "8× 1080p50 + audio embarqué + ANC",
         "slots": [{"video": _fmt(1920, 1080, 50), "audio_count": 1, "anc": True} for _ in range(8)]},
        {"label": "16× 1080p25 + audio embarqué",
         "slots": [{"video": _fmt(1920, 1080, 25), "audio_count": 1, "anc": False} for _ in range(16)]},
        {"label": "4× 1080p50 (2 pistes audio + ANC) + 4× 720p25",
         "slots": ([{"video": _fmt(1920, 1080, 50), "audio_count": 2, "anc": True} for _ in range(4)]
                   + [{"video": _fmt(1280, 720, 25), "audio_count": 1, "anc": False} for _ in range(4)])},
    ],
    "_default": [
        {"label": "4× 1080p50 + audio embarqué + ANC",
         "slots": [{"video": _fmt(1920, 1080, 50), "audio_count": 1, "anc": True} for _ in range(4)]},
        {"label": "1× 1080p50 (sortie unique, test)",
         "slots": [{"video": _fmt(1920, 1080, 50), "audio_count": 1, "anc": True}]},
    ],
}


def _key(node_id, iface):
    return f"tx_layout_{int(node_id)}_{iface}"


def _empty_layout():
    return {"slots": [], "updated_at": None, "updated_by": ""}


def get_layout(node_id, iface):
    """Layout déclaré pour (node_id, iface), ou layout vide si jamais déclaré."""
    from .database import db_get_setting
    if not node_id or not iface:
        return _empty_layout()
    val = db_get_setting(_key(node_id, iface), None)
    if not isinstance(val, dict):
        return _empty_layout()
    out = _empty_layout()
    out.update(val)
    out["slots"] = _normalize_slots(out.get("slots"))
    return out


def _normalize_slots(slots):
    """Normalise une composition de slots.

    ★ Le format vidéo vient désormais d'un PRÉRÉGLAGE de Réglages → Vidéo (`video_formats`), qui porte
    aussi la **chroma** et la **colorimétrie** : on les persiste (`chroma`, `colorimetry`) au lieu de
    les deviner ailleurs. On stocke les **VALEURS**, pas le nom du préréglage : renommer ou supprimer
    une ligne des Réglages ne doit pas casser en silence les modèles/layouts déjà déclarés. `fmt_label`
    n'est qu'un **affichage** (dernier libellé connu) — le front re-résout le préréglage depuis les
    valeurs et signale, en info, un format devenu orphelin de la liste."""
    out = []
    for s in (slots or []):
        if not isinstance(s, dict):
            continue
        v = s.get("video") or {}
        # ★ VIDÉO OPTIONNELLE : un slot peut être vidéo (+ audio/ANC), audio-seul ou ANC-seul. La
        # présence de vidéo se lit sur une largeur > 0 (l'UI d'un slot audio/ANC-seul envoie video=None
        # ou {}). Les slots hérités portent TOUJOURS une vidéo complète → keyer sur w>0 est rétro-sûr.
        has_video = bool(v) and int(v.get("w") or 0) > 0
        audio_count = max(0, int(s.get("audio_count") or 0))
        anc = bool(s.get("anc"))
        # Un slot doit porter AU MOINS une essence : un slot sans vidéo, sans audio et sans ANC ne
        # représente rien (et coûterait une file pour du vide) → on ne le matérialise pas. L'UI
        # empêche déjà de créer un tel slot ; c'est un garde-fou de normalisation, pas un échec muet.
        if not has_video and audio_count == 0 and not anc:
            continue
        if has_video:
            chroma = str(v.get("chroma") or "422")
            video = {
                "w": int(v.get("w")), "h": int(v.get("h") or 1080),
                "fps": float(v.get("fps") or 25), "bd": int(v.get("bd") or 10),
                "scan": "i" if str(v.get("scan") or "p").lower() == "i" else "p",
                "chroma": chroma if chroma in ("420", "422", "444") else "422",
                "colorimetry": str(v.get("colorimetry") or "709").lower(),
            }
        else:
            video = None
        out.append({
            "video": video,                               # None = slot audio-seul / ANC-seul
            "fmt_label": str(s.get("fmt_label") or "") if has_video else "",
            "audio_count": audio_count,
            "anc": anc,
            "label": str(s.get("label") or ""),
        })
    return out


def slot_kind(slot):
    """Essence dominante d'un slot pour l'UI/les libellés : 'video' (peut porter audio/ANC),
    'audio' (audio-seul) ou 'anc' (ANC-seul)."""
    if slot.get("video"):
        return "video"
    if int(slot.get("audio_count") or 0) > 0:
        return "audio"
    return "anc"


def slot_queue_cost(slot):
    """Files RL consommées par un slot : 1 si vidéo + 1/flux audio + 1 si ANC (même modèle de coût que
    le budget RX+TX du contrôleur, cf. controller.py RL_TX_QUEUES_CAP). La vidéo est OPTIONNELLE (slot
    audio-seul / ANC-seul) → 0 file vidéo quand `video` est absent."""
    return (1 if slot.get("video") else 0) + int(slot.get("audio_count") or 0) + (1 if slot.get("anc") else 0)


def _slot_bw_mbps(slot):
    """Débit brut ESTIMÉ (Mb/s) d'un slot vidéo ST 2110-20 non compressé : largeur×hauteur×fps×bpp,
    bpp ≈ 20 bits/pixel en 4:2:2 10 bits (16 en 8 bits) — approximation qui IGNORE le blanking/les
    en-têtes RTP (le vrai débit ligne est ~10-15% plus haut) : sert de GARDE-FOU budget, pas de calcul
    d'ingénierie réseau exact (à confirmer au banc, cf. rapport).

    ★ ENTRELACÉ (scan=='i') : un champ ne transporte que la MOITIÉ des lignes de la trame, donc le
    débit de pixels réel vaut la moitié du progressif de même `fps`. La convention `video_formats`
    stocke le taux de CHAMPS dans `fps` pour l'entrelacé (« 1080i50 » → fps=50, scan='i') : diviser
    par 2 ramène donc 1080i50 à ~1080p25 (≈ la moitié de 1080p50), comme attendu."""
    v = slot.get("video") or {}
    bpp = 20 if int(v.get("bd") or 10) >= 10 else 16
    bw = (int(v.get("w") or 0) * int(v.get("h") or 0) * float(v.get("fps") or 0) * bpp) / 1e6
    if str(v.get("scan") or "p").lower() == "i":
        bw /= 2.0
    # Audio 2110-30 : ~9,2 Mb/s par flux (L24/48k, 8 canaux). Le nombre EXACT de canaux par flux n'est
    # pas porté par le modèle → estimation conservatrice (8ch) pour le garde-fou de lien. Compté même
    # sur une sortie sans vidéo (audio-seul). ANC 2110-40 = variable/bursty et négligeable → ignoré.
    bw += int(slot.get("audio_count") or 0) * (48000 * 24 * 8 / 1e6)
    return bw


def nic_budget(node_id, iface):
    """Budget/capacités de la NIC (node_id, iface) : cap de sessions TX narrow effectif (bibliothèque
    de cartes + profil mesuré, cf. docker_driver._node_rl_tx_cap), modèle, PMD (dpdk|af_xdp|kernel) et
    vitesse de lien. `dpdk_active` = False ⇒ le narrow (RL) est INERTE sur ce port (cf. docs/reference/TX_LAYOUTS.md,
    piège UI relevé par l'utilisateur : `_derive_pacing` n'émet rien sur af_xdp)."""
    from .database import db_get_node_interfaces, db_get_node
    from . import docker_driver as _dd
    row = {}
    for r in (db_get_node_interfaces(node_id) or []):
        if r.get("ifname") == iface:
            row = r
            break
    node = db_get_node(node_id) if node_id else None
    try:
        cap = _dd._node_rl_tx_cap(node) if node else 7
    except Exception:
        cap = 7
    # Défaut = af_xdp (le moteur y retombe quand aucun PMD n'est déclaré) — MÊME convention que
    # `tx_maintenance.port_mode`, sinon la même carte s'affichait « kernel » ici et « af_xdp » là.
    pmd = (row.get("pmd") or "").strip().lower()
    return {
        "model": row.get("model") or "",
        "rl_tx_cap": cap,
        "pmd": pmd or "af_xdp",
        "dpdk_active": pmd in ("dpdk", "sriov"),
        "speed_mbps": int(row.get("speed_mbps") or 0),
    }


def validate_slots(node_id, iface, slots):
    """Valide une composition de layout contre le budget de la carte (queues RL + bande passante
    APPROXIMÉE). `narrow_ok`/le budget de queues sont la vraie contrainte dure ; la bande passante est
    un garde-fou indicatif (cf. `_slot_bw_mbps`). Retourne un dict de diagnostic, jamais d'exception."""
    slots = _normalize_slots(slots)
    budget = nic_budget(node_id, iface)
    used_queues = sum(slot_queue_cost(s) for s in slots)
    used_mbps = sum(_slot_bw_mbps(s) for s in slots)
    errors = []
    warnings = []
    if used_queues > budget["rl_tx_cap"]:
        errors.append(f"{used_queues} files TX requises > cap carte ({budget['rl_tx_cap']}, "
                       f"modèle {budget['model'] or '?'}) — réduire le nombre de slots/flux.")
    if budget["speed_mbps"] and used_mbps > budget["speed_mbps"]:
        errors.append(f"~{used_mbps:.0f} Mb/s estimés > débit du port ({budget['speed_mbps']} Mb/s).")
    if not budget["dpdk_active"]:
        warnings.append("Port en AF-XDP (pas DPDK) : le layout sera déclaré mais restera INACTIF "
                         "(pas d'arbre RL statique tant que ce port n'est pas basculé en DPDK).")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "used_queues": used_queues, "used_mbps": round(used_mbps, 1), **budget}


def set_layout(node_id, iface, slots, actor=None):
    """Sauvegarde le layout (échoue si le budget de queues/bande passante est dépassé — les
    avertissements narrow/af_xdp ne bloquent PAS la sauvegarde, cf. docs/reference/TX_LAYOUTS.md : un layout peut
    être déclaré avant que le port soit basculé en DPDK)."""
    from .database import db_set_setting
    v = validate_slots(node_id, iface, slots)
    if not v["ok"]:
        return False, v
    payload = {"slots": _normalize_slots(slots),
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "updated_by": actor or ""}
    db_set_setting(_key(node_id, iface), payload)
    return True, v


def delete_layout(node_id, iface):
    from .database import db_set_setting
    db_set_setting(_key(node_id, iface), _empty_layout())


def presets_for(node_id, iface):
    """Exemples suggérés pour la carte (non contraignants — l'utilisateur les charge puis ajuste)."""
    budget = nic_budget(node_id, iface)
    model = (budget.get("model") or "").lower()
    for key, presets in PRESETS.items():
        if key != "_default" and key in model:
            return presets
    return PRESETS["_default"]


# ─── Rattachement moteur ↔ NIC + application (provisioning silencieux) ─────────────────────────

def layout_iface_for_container(vmid):
    """NIC média (role=media2110) PRIMAIRE du nœud hébergeant ce moteur — le layout se déclare par
    NIC, un moteur `2110_io` = un nœud (cf. CLAUDE.md : moteur unique par nœud). Multi-NIC : on ne
    retient QUE la primaire (limitation connue, cf. rapport de livraison) — mais la primaire DOIT
    être la même que celle que le moteur reçoit réellement en env `IFACE` (docker_driver._media_ifaces
    place `node.mtl_iface` EN TÊTE, PAS l'ordre brut des lignes `node_interfaces`). Un nœud bi-port
    (2022-7 ou banc, ex. dl360-1 : ens1f0np0 créé AVANT ens1f1np1 en base, mais mtl_iface=ens1f1np1
    = le port DPDK narrow réel) faisait cibler le mauvais port avant ce fix (budget/queues calculés
    sur le mauvais NIC, tx_pins posé sur un port kernel/af_xdp inerte) — banc validation étage 1,
    2026-07-13. On réutilise `_media_ifaces` (même ordre que ce que le moteur reçoit) plutôt que de
    dupliquer une requête brute sur `node_interfaces`."""
    from .database import db_get_container, db_get_node
    from . import docker_driver as _dd
    c = db_get_container(vmid) or {}
    node_id = c.get("node_id")
    if node_id is None:
        return None, None
    node = db_get_node(node_id) or {}
    mifs = _dd._media_ifaces(node) or []
    if not mifs:
        return node_id, None
    return node_id, mifs[0].get("ifname")


def engine_ports(vmid):
    """(node_id, [ifname, …]) — TOUS les ports média du nœud qui héberge ce moteur, dans l'ordre vu
    par le moteur (primaire en tête). Contrairement à `layout_iface_for_container` qui ne rend que la
    primaire, ceci est la vue à utiliser dès qu'on raisonne « par carte » : sur un nœud bi-port, le
    moteur émet RÉELLEMENT sur les deux, et ne regarder que la primaire rendait la seconde carte
    invisible à toute la chaîne modèle/layout (layout déclaré jamais appliqué, « aucun moteur déployé
    sur cette carte », bouton Appliquer grisé — bug 2026-07-27)."""
    from .database import db_get_container, db_get_node
    from . import docker_driver as _dd
    c = db_get_container(vmid) or {}
    node_id = c.get("node_id")
    if node_id is None:
        return None, []
    node = db_get_node(node_id) or {}
    return node_id, [e["ifname"] for e in (_dd._media_ifaces(node) or [])]


def engine_units(vmid):
    """(node_id, [unité, …]) — unités de CAPACITÉ du moteur : port autonome ou paire 2022-7
    (cf. `docker_driver.media_capacity_units`). C'est la granularité de DÉCLARATION et de BUDGET :
    `engine_ports` ne sert plus qu'à répondre « ce moteur touche-t-il cette carte ? »."""
    from .database import db_get_container, db_get_node
    from . import docker_driver as _dd
    c = db_get_container(vmid) or {}
    node_id = c.get("node_id")
    if node_id is None:
        return None, []
    return node_id, _dd.media_capacity_units(db_get_node(node_id) or {})


def port_slots(vmid, iface, params=None, role="tx"):
    """Indices des slots du moteur que le port `iface` porte RÉELLEMENT, dans l'ordre croissant.

    Répartition EFFECTIVE au sens du moteur : épinglages `tx_pins` s'ils existent, sinon modulo
    (cf. `docker_driver.engine_slot_ports`). C'est l'état CONSTATÉ — ce que le moteur fait
    aujourd'hui — à distinguer de l'état VOULU par les modèles de carte, que calcule
    `plan_port_slots`. Le k-ième slot déclaré d'un layout de port vit à l'indice `port_slots(...)[k]`,
    PAS à l'indice k du moteur (l'ancienne confusion ordinal↔index n'était juste que sur un nœud
    mono-port).

    Borné par les sorties ACTIVES (`active_tx_count`), pas par la réserve structurelle `tx_slots`
    (25 slots pré-provisionnés) : décrire la réserve laisserait croire à une capacité de 25 sorties
    par carte, ce qui est faux — le vrai plafond est le budget de files RL du port. Même raison que
    le filtrage « hors layout » de `layout_status`."""
    import json as _json
    from .database import db_get_container, db_get_node
    from . import docker_driver as _dd
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id")) or {}
    if params is None:
        try:
            params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:
            params = {}
    if role == "tx":
        n = _dd._pcount(params, "active_tx_count", 0)
        n = min(n, len(params.get("tx_slots") or []) or n)
    else:
        n = _dd._pcount(params, "active_rx_count", 0)
    # `iface` peut être un ifname quelconque de l'unité (l'exploitant clique un port) : on raisonne
    # sur l'UNITÉ. En 2022-7, les deux legs portent les MÊMES slots — demander « quels slots porte
    # le leg blue ? » doit rendre tous les slots de la paire, pas la moitié.
    unit = _dd.unit_of_iface(node, iface)
    ifaces = set(unit["ifaces"]) if unit else {iface}
    _, resolve = _dd.engine_slot_ports(node, params, "tx_pins" if role == "tx" else "rx_pins")
    return [i for i in range(n) if resolve(i) in ifaces]


def plan_port_slots(ports, want, owned):
    """ALLOCATEUR d'indices de slots TX par port — fonction PURE (aucune DB, aucun I/O).

    Modèle multi-port retenu (décision utilisateur 2026-07-27) : **le modèle de carte décide**. Chaque
    port déclare combien de sorties il émet ; le total du moteur en découle, et `tx_pins` est écrit
    pour réaliser exactement cette répartition (on n'utilise donc plus le modulo dès qu'un modèle est
    déclaré).

    Deux contraintes non négociables :

    1. **STABILITÉ.** Un indice de slot porte son adresse multicast ET son câblage (`tx<i>_shm`).
       Renuméroter en aveugle recâblerait des sorties en production. On garde donc à chaque port les
       indices qu'il possède DÉJÀ (`owned`), et on ne pioche que pour le delta.
    2. **CONTIGUÏTÉ.** Le contrôleur itère `range(active_tx_count)` : les indices attribués doivent
       couvrir exactement `0..total-1`, sans trou — sinon un slot déclaré au-delà du compte ne serait
       jamais émis (et personne ne le dirait).

    `ports` = ifnames dans l'ordre moteur ; `want` = {ifname: nb de sorties déclarées, ou **None**
    si ce port n'a AUCUN modèle} ; `owned` = {ifname: [indices actuellement portés]}.
    Retourne `(pins, total)` avec `pins = {str(index): ifname}` et `total` = sorties actives à poser.

    ★ `None` (pas de modèle) ≠ `0` (modèle vide, l'exploitant veut zéro sortie sur ce port). Sans
    cette distinction, déclarer un modèle sur UNE carte viderait sa voisine muette : sur dl360-1,
    appliquer les 4 sorties de `ens1f0np0` aurait rapatrié chez lui le slot 0 — celui câblé sur
    `mixer_pgm` — qui émettait sur `ens1f1np1`. Un port sans modèle CONSERVE ce qu'il porte."""
    want = {p: (len(owned.get(p) or []) if want.get(p) is None else max(0, int(want[p])))
            for p in ports}
    total = sum(want.values())
    # 1) On CONSERVE, port par port, les indices déjà portés qui tiennent dans le nouveau total.
    #    Trié : un port qui rétrécit garde ses PREMIERS slots (les plus anciens, donc les câblés).
    keep, taken = {}, set()
    for p in ports:
        k = [i for i in sorted(owned.get(p) or []) if 0 <= i < total and i not in taken][:want[p]]
        keep[p] = k
        taken.update(k)
    # 2) Les indices restants du pool 0..total-1 comblent les ports incomplets, dans l'ordre moteur.
    free = [i for i in range(total) if i not in taken]
    pins = {}
    for p in ports:
        need = want[p] - len(keep[p])
        got = keep[p] + [free.pop(0) for _ in range(min(need, len(free)))]
        for i in got:
            pins[str(i)] = p
    return pins, total


def _plan_apply(vmid, iface=None, params=None, slots_decl=None):
    """PLANIFICATEUR COMMUN — `(node_id, decl_by_unit, tx_pins, total)`. Ne lit rien du réseau, n'écrit
    rien : décide seulement QUELLES unités sont ciblées, avec quelles sorties déclarées, à quels
    indices de slot, et pour quel total.

    Point d'entrée UNIQUE de `apply_layout`, `preview_layout_params` et `planned_active_tx`. Ces trois
    doivent répondre la même chose : si le pré-vol calcule autrement que l'application, le verdict de
    maintenance annonce une opération et une autre se produit. Toute la journée du 2026-07-27 a été
    faite de ce genre d'écarts (résolveur slot→port dupliqué, cap de files relu de la demande, format
    global vs par slot) — on n'en ajoute pas un quatrième.

    `iface` → cible cette unité seule (les autres conservent leurs sorties) ; None → toutes celles qui
    déclarent un modèle. `slots_decl` → layout BROUILLON substitué à celui enregistré, pour chiffrer
    une édition en cours."""
    from .database import db_get_node
    from . import docker_driver as _dd
    node_id, units = engine_units(vmid)
    if not units:
        return None, {}, {}, 0
    keys = [u["key"] for u in units]
    if iface:
        unit = _dd.unit_of_iface(db_get_node(node_id) or {}, iface)
        targets = [unit["key"]] if unit else []
    else:
        targets = [k for k in keys if (get_layout(node_id, k) or {}).get("slots")]
    decl_by_unit = {}
    for k in targets:
        d = _normalize_slots(slots_decl) if slots_decl is not None \
            else ((get_layout(node_id, k) or {}).get("slots") or [])
        if d:
            decl_by_unit[k] = d
    # `None` = « conserve » (≠ 0, qui viderait l'unité) — y compris pour une cible SANS modèle :
    # `apply_layout` refuse ce cas, donc le plan doit dire « rien ne change ».
    want = {k: (len(decl_by_unit[k]) if k in decl_by_unit else None) for k in keys}
    owned = {k: port_slots(vmid, k, params=params) for k in keys}
    tx_pins, total = plan_port_slots(keys, want, owned)
    return node_id, decl_by_unit, tx_pins, total


def planned_active_tx(vmid, iface=None):
    """`active_tx_count` que produirait `apply_layout(vmid, iface)` — SANS rien écrire.

    Sert au pré-vol de la route (verdict de maintenance) : le nombre de sorties après application
    est le TOTAL du nœud, pas le compte de la seule carte cliquée. Comparer le modèle d'une carte au
    budget bootté du moteur ferait annoncer une recréation là où il n'y en a pas (ou l'inverse) dès
    qu'un nœud a deux cartes."""
    import json as _json
    from .database import db_get_container
    c = db_get_container(vmid) or {}
    try:
        params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        params = {}
    return _plan_apply(vmid, iface=iface, params=params)[3]


def layout_status_all(vmid):
    """État du layout de CHAQUE port média du moteur : `{ifname: layout_status(vmid, ifname)}`.
    Vue à privilégier sur un nœud multi-port — `layout_status(vmid)` seul ne parle que de la carte
    primaire et laisse croire que la seconde n'a rien (elle porte pourtant la moitié des sorties)."""
    node_id, ifaces = engine_ports(vmid)
    if not ifaces:
        return {}
    return {i: layout_status(vmid, i) for i in ifaces}


def layout_status(vmid, iface=None):
    """État du layout d'UN port du moteur `vmid` (affichage lecture seule de Destinations 2110) :
    'no_iface' (pas de NIC média identifiée), 'none' (rien de déclaré), 'pending' (déclaré mais pas
    encore appliqué / partiellement), 'applied' (tous les slots déclarés ont une destination
    provisionnée).

    `iface=None` → carte PRIMAIRE (compat des appelants historiques). Sur un nœud multi-port,
    préférer `layout_status_all`. Les indices renvoyés (`slot_states`, `owned_slots`) sont ceux du
    MOTEUR, pas les ordinaux du layout : le k-ième slot déclaré vit à l'indice `owned_slots[k]`
    (cf. `port_slots` — la répartition entre ports reste automatique)."""
    import json as _json
    from .database import db_get_container
    if iface is None:
        node_id, iface = layout_iface_for_container(vmid)
    else:
        node_id, _ifs = engine_ports(vmid)
        if iface not in _ifs:
            return {"state": "no_iface", "iface": iface}
    if not iface:
        return {"state": "no_iface", "iface": None}
    layout = get_layout(node_id, iface)
    slots_decl = layout.get("slots") or []
    budget = nic_budget(node_id, iface)
    if not slots_decl:
        return {"state": "none", "iface": iface, "budget": budget, "node_id": node_id}
    c = db_get_container(vmid) or {}
    try:
        dc = _json.loads(c.get("deploy_config") or "{}") or {}
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    tx_slots = params.get("tx_slots") or []
    # Slots du MOTEUR que CE port porte (répartition auto + épinglages). Le k-ième slot déclaré du
    # layout décrit `owned[k]` — l'ancien code lisait `tx_slots[k]`, ce qui ne coïncide QUE sur un
    # nœud mono-port ; sur bi-port il décrivait les sorties de l'autre carte.
    owned = port_slots(vmid, iface, params=params)
    provisioned = sum(1 for k in range(min(len(slots_decl), len(owned)))
                      if (tx_slots[owned[k]] if owned[k] < len(tx_slots) else {}).get("multicast_ip")
                      and (tx_slots[owned[k]] if owned[k] < len(tx_slots) else {}).get("dest_port"))
    # Modèle multi-port (décision utilisateur 2026-07-27) : le MODÈLE DÉCIDE combien de sorties le
    # port émet, `plan_port_slots` alloue les indices et `tx_pins` réalise la répartition. Déclarer
    # plus de sorties que le port n'en porte AUJOURD'HUI est donc l'état normal d'un modèle pas
    # encore appliqué — « pending », pas une erreur (c'est appliquer qui créera les slots manquants).
    if provisioned >= len(slots_decl) and provisioned:
        state = "applied"
    elif provisioned:
        state = "pending"
    else:
        state = "none"
    # ÉTAT PAR SORTIE (étage 2) — sans ça, personne ne comprend pourquoi ALLUMER une sortie est
    # gratuit : c'est parce qu'elle EXISTAIT DÉJÀ (session + feuille RL créées, silencieuses).
    #   · 'active'       : session vivante ET alimentée (elle émet du contenu)
    #   · 'provisioned'  : session vivante mais SANS source → silencieuse (l'activer = swap, 0 commit)
    #   · 'declared'     : déclarée au layout mais pas encore provisionnée (→ « Appliquer le layout »)
    #   · 'out_of_layout': slot du moteur hors du layout de la carte (l'armer recale l'arbre)
    slot_states = {}
    try:
        from . import tx_maintenance as _txm
        live = {}
        for s in _txm.tx_sessions(vmid, params):
            if s["essence"] == "video":
                live[s["slot"]] = ("provisioned" if s.get("silent") else "active")
        # On affiche les sorties du MODÈLE (déclarées) + les slots RÉELLEMENT actifs hors modèle (une
        # anomalie à surfacer). PAS la réserve structurelle vide (tx_count) au-delà du modèle : montrer
        # 28 slots « hors layout » vides laissait croire à une limite/capacité de 32, ce qui est FAUX
        # (le vrai plafond = files RL de la carte). Un slot au-delà du modèle n'apparaît que s'il ÉMET.
        # États indexés par slot MOTEUR, restreints aux slots de CE port : un slot de l'autre carte
        # n'est ni « déclaré » ni « hors layout » ici — il relève du layout de sa propre carte.
        _decl_idx = set(owned[:len(slots_decl)])
        for i in owned:
            if i in _decl_idx:
                slot_states[i] = live.get(i, "declared")
            elif i in live:                       # actif hors modèle = orphelin réel (à recaler)
                slot_states[i] = "out_of_layout"
    except Exception as e:
        log.warning("layout_status %s: états par sortie: %s", vmid, e)
    # Budget de files RL consommé par le layout DÉCLARÉ (vidéo + audio + ANC) sur le plafond DE CE
    # PORT. Chaque port a son propre arbre RL et son propre cap : compter les sorties des deux
    # cartes sur un seul port doublait la consommation affichée (rouge à tort).
    used_queues = sum(slot_queue_cost(s) for s in slots_decl)
    return {"state": state, "iface": iface, "node_id": node_id, "budget": budget,
            "declared": len(slots_decl), "provisioned": provisioned,
            "owned_slots": owned, "capacity_slots": len(owned),
            "used_queues": used_queues, "slot_states": slot_states,
            "layout_updated_at": layout.get("updated_at")}


def preview_layout_params(vmid, params, slots_decl=None, iface=None):
    """Params que `apply_layout` PRODUIRAIT, SANS rien allouer ni persister (dry-run) — sert au verdict
    de pré-vol de l'étage 2 (`tx_maintenance.classify`). Les destinations manquantes sont simulées par
    une adresse factice (l'adresse réelle sera allouée à l'application) : ce qui compte pour le verdict
    est QUELLES sessions vont être créées (donc quels ports vont recaler leur arbre), pas leur adresse.

    `slots_decl` (optionnel) = layout BROUILLON (édition en cours, pas encore enregistrée) : sert au
    chiffrage AVANT le clic (« ce que vous vous apprêtez à changer coûte N commit(s) et fige ces
    sorties »). Sans lui, on prend le layout enregistré de la NIC."""
    out = dict(params or {})
    # MÊME planificateur que `apply_layout` : mêmes unités ciblées, mêmes indices, même total.
    node_id, decl_by_unit, tx_pins, total = _plan_apply(vmid, iface=iface, params=out,
                                                        slots_decl=slots_decl)
    if not decl_by_unit:
        return params
    tx_slots = [dict(s or {}) for s in (out.get("tx_slots") or [])]
    while len(tx_slots) < total:
        tx_slots.append({})
    # Sorties qui DISPARAISSENT (le total rétrécit) : vidées ici aussi, sinon le verdict compterait
    # des sessions que l'application supprimerait.
    from . import docker_driver as _dd0
    for i in range(total, max(_dd0._pcount(out, "active_tx_count", 0), 0)):
        if i < len(tx_slots):
            tx_slots[i] = {}
        out.pop(cle_tx_shm(i), None)
    _pairs = [(i, decl_by_unit[u][k])
              for u in decl_by_unit
              for k, i in enumerate(sorted(int(x) for x, uu in tx_pins.items() if uu == u))
              if k < len(decl_by_unit[u])]
    for i, decl in _pairs:
        slot = tx_slots[i]
        v = decl.get("video") or None
        if v:
            slot.pop("video_off", None)
            if not (slot.get("multicast_ip") and slot.get("dest_port")):
                slot["multicast_ip"], slot["dest_port"] = "0.0.0.%d" % (i + 1), 20000 + i
            for k_decl, k_slot in (("w", "width"), ("h", "height"), ("fps", "fps"),
                                   ("bd", "bit_depth"), ("scan", "scan")):
                if v.get(k_decl):
                    slot[k_slot] = v[k_decl]
        else:
            # Slot audio-seul / ANC-seul : aucune destination/format vidéo simulé (symétrique d'apply_layout).
            slot["video_off"] = True
            for _k in ("multicast_ip", "dest_port", "multicast_ip_leg1", "dest_port_leg1",
                       "width", "height"):
                slot.pop(_k, None)
        naud = int(decl.get("audio_count") or 0)
        audios = [dict(a or {}) for a in (slot.get("audios") or [])]
        while len(audios) < naud:
            audios.append({"multicast_ip": "0.0.1.%d" % len(audios), "dest_port": 21000 + len(audios)})
        # naud == 0 signifie ZÉRO audio (sortie vidéo seule) : on tronque à naud, PAS de repli sur la
        # liste existante — un `audios[:naud] if naud else audios` gardait les audios déjà là quand on
        # demandait 0, d'où le « je mets 0 audios et il en crée quand même ».
        slot["audios"] = audios[:naud]
        if decl.get("anc"):
            if not (slot.get("anc_multicast_ip") and slot.get("anc_dest_port")):
                slot["anc_multicast_ip"], slot["anc_dest_port"] = "0.0.2.%d" % i, 22000 + i
        else:
            for _k in ("anc_multicast_ip", "anc_dest_port",
                       "anc_multicast_ip_leg1", "anc_dest_port_leg1"):
                slot.pop(_k, None)
        tx_slots[i] = slot
    out["tx_slots"] = tx_slots
    out["tx_pins"] = tx_pins
    # Le total des modèles fait autorité → le verdict (tx_maintenance.classify → tx_sessions, borné
    # par active_tx_count) compte les sessions réelles de l'état APRÈS, y compris en RÉDUCTION.
    out["active_tx_count"] = total
    out["tx_count"] = max(_dd0._pcount(out, "tx_count", 0), total)
    return out


def apply_layout(vmid, iface=None, redeploy=False):
    """APPLIQUE les modèles de carte déclarés à un moteur déployé.

    `iface=None` → applique les modèles de TOUTES les unités qui en déclarent un (chemin
    déploiement/`resync_moteur`). `iface` renseigné → applique le modèle de CETTE unité seulement ;
    les autres conservent exactement leurs sorties (règle `None` de `plan_port_slots`) — c'est la
    portée « par carte, effet nœud annoncé » retenue avec l'utilisateur le 2026-07-27.

    Modèle multi-port : le modèle DÉCIDE combien de sorties son unité émet. `plan_port_slots` alloue
    les indices (stables : un indice porte son multicast ET son câblage), `tx_pins` réalise la
    répartition, et `active_tx_count` devient la somme.

    **Ne recrée PAS le moteur par défaut** (décision utilisateur) : `active_tx_count` étant figé dans
    l'env au `docker run`, un changement du nombre de sorties n'est effectif qu'après recréation —
    donc après coupure de TOUS les flux du nœud. On écrit la déclaration, on laisse le moteur
    tourner, et `docker_driver.reconcile_engine_sizing` signale « redéploiement requis » : l'écart
    n'est plus silencieux, et l'exploitant choisit son moment. `redeploy=True` recrée tout de suite.

    Retourne `(True, infos)` — `infos.removed` nomme les sorties supprimées (adresse + source câblée
    perdue), `infos.redeploy_required` dit si le moteur doit être recréé pour que ça prenne effet."""
    import json as _json
    from .database import db_get_container, db_update_deploy_config, db_add_alert
    from . import allocations as _alloc
    from . import docker_driver as _dd

    c = db_get_container(vmid) or {}
    if not c:
        return False, "container introuvable"
    node_id, units = engine_units(vmid)
    if not units:
        return False, "aucune NIC média (role=media2110) identifiée pour ce nœud"

    try:
        dc = _json.loads(c.get("deploy_config") or "{}") or {}
    except Exception:
        dc = {}
    ctype = dc.get("type") or "2110_io"
    params = dict(dc.get("params") or {})

    # ── Plan (unités ciblées + indices alloués + total) : planificateur COMMUN au pré-vol ─────────
    if iface and not _dd.unit_of_iface(_dd.db_get_node(node_id) or {}, iface):
        return False, f"« {iface} » n'est pas une NIC média de ce nœud"
    _nid, decl_by_unit, tx_pins, total = _plan_apply(vmid, iface=iface, params=params)
    if not decl_by_unit:
        return False, "aucun modèle déclaré pour cette carte (Réglages → Réseau → Interfaces)"
    targets = list(decl_by_unit)
    old_atx = _dd._pcount(params, "active_tx_count", 0)

    tx_slots = [dict(s or {}) for s in (params.get("tx_slots") or [])]
    while len(tx_slots) < total:
        tx_slots.append({})

    # ── Sorties SUPPRIMÉES (le total rétrécit) : nommées, jamais perdues en silence ───────────────
    removed = []
    for i in range(total, max(old_atx, 0)):
        s = tx_slots[i] if i < len(tx_slots) else {}
        src = _slot_source(params, i)
        if s.get("multicast_ip") or src:
            removed.append({"slot": i, "multicast_ip": s.get("multicast_ip") or "",
                            "dest_port": s.get("dest_port"), "source": src})
        params.pop(cle_tx_shm(i), None)          # câblage perdu avec la sortie
        if i < len(tx_slots):
            tx_slots[i] = {}

    applied = 0
    for ukey, slots_decl in decl_by_unit.items():
        if not slots_decl:
            continue
        mine = sorted(int(i) for i, u in tx_pins.items() if u == ukey)
        for k in range(min(len(slots_decl), len(mine))):
            i = mine[k]
            decl = slots_decl[k]
            slot = dict(tx_slots[i] or {})
            # Le multicast est alloué avec l'ifname CANONIQUE de l'unité : une plage déclarée par
            # interface (`mcast_ranges.ifname`) doit être celle du port qui émet réellement.
            iface_alloc = ukey
            v = decl.get("video") or None
            if v:
                slot.pop("video_off", None)   # (re)devenu un slot vidéo
                if not (slot.get("multicast_ip") and slot.get("dest_port")):
                    mcast, port = _alloc.allocate_multicast_for(
                        node_id, iface_alloc, essence="video", owner_ref=f"tx:{vmid}:{i}:video:layout",
                        slot=i)
                    if mcast:
                        slot["multicast_ip"], slot["dest_port"] = mcast, port
                if v.get("w"):    slot["width"] = int(v["w"])
                if v.get("h"):    slot["height"] = int(v["h"])
                if v.get("fps"):  slot["fps"] = v["fps"]
                if v.get("bd"):   slot["bit_depth"] = int(v["bd"])
                if v.get("scan"): slot["scan"] = v["scan"]
            else:
                # Slot audio-seul / ANC-seul : pas de destination ni de format vidéo → le contrôleur n'émet
                # AUCUNE session vidéo pour ce slot (l'émission vidéo exige mcast+port vidéo, cf.
                # controller.py). On purge une éventuelle destination vidéo héritée et on POSE le marqueur
                # `video_off` — signal explicite (≠ « mcast pas encore alloué ») lu par le builder NMOS pour
                # ne PAS enregistrer de sender vidéo fantôme sur ce slot.
                slot["video_off"] = True
                for _k in ("multicast_ip", "dest_port", "multicast_ip_leg1", "dest_port_leg1",
                           "width", "height"):
                    slot.pop(_k, None)

            naud = int(decl.get("audio_count") or 0)
            audios = list(slot.get("audios") or [])
            while len(audios) < naud:
                ai = len(audios)
                m2, p2 = _alloc.allocate_multicast_for(
                    node_id, iface_alloc, essence="audio", owner_ref=f"tx:{vmid}:{i}:audio:{ai}:layout",
                    slot=i, sub_index=ai)
                audios.append({"multicast_ip": m2, "dest_port": p2} if m2 else {})
            # naud == 0 → zéro audio (sortie vidéo seule) : troncature stricte, jamais de repli sur la liste
            # existante (cf. même correctif dans preview_layout_params).
            slot["audios"] = audios[:naud]

            if decl.get("anc"):
                if not (slot.get("anc_multicast_ip") and slot.get("anc_dest_port")):
                    m3, p3 = _alloc.allocate_multicast_for(
                        node_id, iface_alloc, essence="anc", owner_ref=f"tx:{vmid}:{i}:anc:layout",
                        slot=i)
                    if m3:
                        slot["anc_multicast_ip"], slot["anc_dest_port"] = m3, p3
            else:
                # ANC désactivée dans le modèle → retirer la destination ANC (sinon la session ANC restait
                # provisionnée : symétrique du bug audio ci-dessus, « je mets 0 ANC et il en crée quand même »).
                for _k in ("anc_multicast_ip", "anc_dest_port",
                           "anc_multicast_ip_leg1", "anc_dest_port_leg1"):
                    slot.pop(_k, None)

            tx_slots[i] = slot
            applied += 1

    params["tx_slots"] = tx_slots
    params["tx_pins"] = tx_pins
    # Le total des modèles fait autorité sur le nombre de sorties. `tx_count` = capacité STRUCTURELLE
    # (réserve de slots pré-provisionnés), toujours ≥ active.
    params["active_tx_count"] = total
    params["tx_count"] = max(_dd._pcount(params, "tx_count", 0), total)
    # ★ tx_flows fait AUTORITÉ : le hook before_deploy (plugins/2110_io/hooks.py) RE-DÉRIVE
    # active_tx_count depuis tx_flows au (re)déploiement. Poser active_tx_count seul ne suffit donc
    # PAS — il serait écrasé par l'ancien tx_flows (bug vécu : modèle 32 sorties → active_tx_count
    # retombait à 6). On REGÉNÈRE tx_flows depuis le layout matérialisé (video_off respecté).
    from . import io2110_flows as _iof
    params["tx_flows"] = _iof.derive_tx_flows(params)
    db_update_deploy_config(vmid, ctype, params)

    # ── Effet sur le moteur ───────────────────────────────────────────────────────────────────────
    # ACTIVE_TX_COUNT est FIGÉ dans l'env au `docker run` : changer le NOMBRE de sorties (grandir ou
    # réduire) n'est effectif qu'après recréation du conteneur, donc après coupure de TOUS les flux
    # 2110 du nœud. DÉCISION UTILISATEUR (2026-07-27) : on ne recrée PAS d'office. On écrit la
    # déclaration, le moteur continue de tourner, et `docker_driver.reconcile_engine_sizing` signale
    # « moteur dimensionné sur une configuration périmée — redéploiement requis ». L'exploitant
    # choisit son moment ; l'écart n'est plus silencieux (c'est ce détecteur qui rend ce report sûr).
    # `redeploy=True` = l'exploitant a demandé « appliquer ET redéployer maintenant ».
    booted = _dd.engine_booted_active_tx(vmid)
    redeploy_required = (total != booted) if booted is not None else False
    did_redeploy = False
    try:
        if redeploy_required and redeploy:
            _dd.deploy_docker(vmid, params, type_script="2110_io")
            did_redeploy = True
            try:
                from services import nmos as _nmos
                _nmos.notify_state_change()
            except Exception:
                pass
        elif not redeploy_required:
            # Le compte ne bouge pas → application À CHAUD (provisioning des sessions déclarées).
            # C'est l'événement de maintenance de l'étage 2 (recalcul de l'arbre RL du port), gaté
            # en amont par la route ; ne PAS pousser quand le compte change, ce serait mi-appliqué.
            _dd.push_tx_slots(vmid, params)
    except Exception as e:
        log.warning("apply_layout %s: apply (%s): %s", vmid,
                    "redeploy" if redeploy else "push", e)

    _scope = iface or "toutes les cartes"
    # ★ CHIFFRER LA CONSÉQUENCE du redéploiement qu'on réclame. Le 2026-07-27, l'alerte disait
    # « redéploiement requis » sans dire À QUOI le moteur reviendrait : l'exploitant a cliqué sur la
    # seule action proposée, et le moteur est reparti avec 64 sorties pour 16 lcores — 6 RX mortes.
    # Une alerte qui demande un geste disruptif doit en annoncer le résultat.
    _short = None
    try:
        _besoin, _cap, _trop = _dd.lcore_demand(_dd.db_get_node(node_id) or {}, params)
        if _trop:
            _short = (_besoin, _cap)
    except Exception as e:
        log.warning("apply_layout %s: pré-vol lcores: %s", vmid, e)
    _non_effectif = bool(redeploy_required and not did_redeploy)
    # Choix de la clé i18n complète selon la combinaison (jamais de demi-phrase composée en
    # paramètre) : présence de sorties supprimées × non-effectif × conséquence du redéploiement.
    if _short:
        _queue = "non_effectif_court" if _non_effectif else "court"
    elif _non_effectif:
        _queue = "non_effectif_chaud"
    elif did_redeploy:
        _queue = "recree"
    else:
        _queue = "chaud"
    _clef = "alert.deploy.tx_layout_%s%s" % ("suppr_" if removed else "", _queue)
    _params = {"h": c.get("hostname") or vmid, "scope": _scope, "applied": applied, "total": total}
    if removed:
        _params["n_removed"] = len(removed)
        _params["removed_list"] = ", ".join(
            "TX #%s %s%s" % (r["slot"], r["multicast_ip"] or "—",
                              " ← %s" % r["source"] if r["source"] else "")
            for r in removed)
    if _non_effectif:
        _params["booted"] = booted
    if _short:
        _params["needed"] = _short[0]
        _params["cap"] = _short[1]
    db_add_alert(_clef, "warning" if (removed or _non_effectif) else "info",
                 vmid=vmid, kind="tx_stall", params=_params)
    return True, {"applied": applied, "iface": iface, "units": targets,
                  "active_tx_count": total, "tx_pins": tx_pins, "removed": removed,
                  "redeploy_required": redeploy_required and not did_redeploy,
                  "lcore_shortfall": ({"needed": _short[0], "cap": _short[1]} if _short else None),
                  "recreated": did_redeploy}


# ─── « Modèle d'utilisation de la carte » (vue unique, Réglages) ───────────────────────────────────
# Toutes les briques du chantier existaient — éparpillées (marquage ambre sur Câbles, modale de
# format, bac de maintenance, format par slot). Personne ne pouvait VOIR le modèle d'une carte :
# combien de sorties sont DÉCLARÉES, dans quel format ANNONCÉ (contrat SDP), lesquelles émettent
# vraiment, et ce qui coûte un `rte_tm_hierarchy_commit` (= gel ~1 s de TOUT le port) vs ce qui est
# gratuit. `card_model` assemble cette vue en UN appel (le front faisait sinon N fetchs).

def engine_for_card(node_id, iface):
    """Moteur `2110_io` déployé sur ce nœud et qui ÉMET sur la NIC `iface` (un moteur par nœud, cf.
    CLAUDE.md). Retourne le dict container, ou None.

    ★ Match sur TOUS les ports média du moteur, pas seulement la primaire. Avant, un nœud bi-port
    faisait répondre None pour la seconde carte alors que le moteur y émettait la moitié de ses
    sorties : la page Modèles affichait « Aucun moteur 2110 déployé sur cette carte » et DÉSACTIVAIT
    « Appliquer » — impasse totale (la page Destinations, elle, renvoie vers cette page en DPDK)."""
    import json as _json
    from .database import db_get_containers
    for c in db_get_containers() or []:
        if c.get("node_id") != node_id:
            continue
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") != "2110_io":
            continue
        _nid, _ifs = engine_ports(c["vmid"])
        if iface in _ifs:
            return c
    return None


def _slot_source(params, i):
    """Source réellement câblée sur la sortie #i (shm MXL), ou "" si la sortie est silencieuse."""
    return str((params or {}).get(cle_tx_shm(i)) or "").strip()


def _slot_name(params, i):
    from . import io2110_flows as _iof
    try:
        for f in _iof.active_flows(params or {}, "tx"):
            if f.get("essence") == "video" and int(f.get("idx") or 0) == i and (f.get("label") or "").strip():
                return f["label"].strip()
    except Exception:
        pass
    return ""


def card_model(node_id, iface):
    """Vue complète du modèle d'utilisation d'une carte (node_id, iface) — LECTURE SEULE.

    Ce que l'exploitant doit lire d'un coup d'œil :
      · `port`      : modèle, PMD, **mode de pacing** — `rl=True` (DPDK) ⇒ déclarer une sortie coûte
                      un commit TM = gel ~1 s de TOUT le port ; `rl=False` (AF-XDP) ⇒ **aucun commit
                      possible**, le modèle ne coûte RIEN (l'UI doit le DIRE, pas menacer d'un coût
                      inexistant) ;
      · `slots`     : les sorties du port, avec leur **format annoncé** (contrat SDP), leur **état**
                      (`active` / `provisioned` = déclarée-silencieuse / `declared` = pas encore
                      provisionnée / `out_of_layout`), leur **source** courante et leur destination ;
      · `budget`    : files RL consommées / plafond mesuré de la carte ;
      · `pending`   : le bac de maintenance du moteur (changements groupés, un seul blip).
    """
    import json as _json
    from .database import db_get_node
    from . import docker_driver as _dd
    # UNITÉ de capacité de cette carte : port autonome, ou paire 2022-7 (les deux legs portent le
    # MÊME flux → une capacité, pas deux). L'ifname reçu peut être n'importe lequel de ses legs.
    _unit = _dd.unit_of_iface(db_get_node(node_id) or {}, iface) or {
        "key": iface, "ifaces": [iface], "kind": "port", "label": iface}
    budget = nic_budget(node_id, iface)
    layout = get_layout(node_id, _unit["key"])
    decl = layout.get("slots") or []
    c = engine_for_card(node_id, iface)
    out = {
        "node_id": node_id, "iface": iface,
        "unit": _unit,
        "port": {"iface": iface, "model": budget.get("model") or "",
                 "pmd": budget.get("pmd"), "rl": bool(budget.get("dpdk_active")),
                 "speed_mbps": budget.get("speed_mbps") or 0,
                 "rl_tx_cap": budget.get("rl_tx_cap") or 0},
        "budget": budget,
        "layout": {"slots": decl, "updated_at": layout.get("updated_at"),
                   "updated_by": layout.get("updated_by") or ""},
        "used_queues": sum(slot_queue_cost(s) for s in decl),
        "presets": presets_for(node_id, iface),
        "engine": None, "slots": [], "pending": [],
        # Rattachement au MODÈLE de carte dont ce layout est issu (bibliothèque, app/tx_card_models.py).
        # Le modèle est une SOURCE ; ce layout reste la VÉRITÉ — d'où `diverged`, qu'on AFFICHE.
        "binding": {"model": None, "diverged": False},
    }
    try:
        from . import tx_card_models as _tcm
        out["binding"] = _tcm.card_binding(node_id, iface)
    except Exception as e:
        log.warning("card_model %s/%s: rattachement modèle: %s", node_id, iface, e)
    if not c:
        # Sans moteur déployé, le modèle reste ÉDITABLE (on déclare avant de déployer) — mais on ne
        # peut ni l'appliquer ni afficher d'état par sortie : on le dit au lieu d'afficher du vide.
        out["slots"] = [{"idx": i, "declared": d, "state": "declared", "source": "", "name": "",
                         "dest": None, "queues": slot_queue_cost(d)} for i, d in enumerate(decl)]
        return out

    vmid = c["vmid"]
    try:
        dc = _json.loads(c.get("deploy_config") or "{}") or {}
    except Exception:
        dc = {}
    params = dc.get("params") or {}
    tx_slots = params.get("tx_slots") or []
    st = layout_status(vmid, _unit["key"]) or {}
    # Slots du MOTEUR que cette unité porte réellement (répartition auto + épinglages) : c'est ce qui
    # explique « porte N sortie(s) » et d'où sortent les indices affichés. Sans ça, l'exploitant lit
    # des numéros de sortie sans savoir à quoi ils se rattachent.
    out["owned_slots"] = st.get("owned_slots") or []
    out["planned_active_tx"] = planned_active_tx(vmid, _unit["key"])
    try:
        _booted = _dd.engine_booted_active_tx(vmid)
    except Exception:
        _booted = None
    slot_states = st.get("slot_states") or {}
    out["engine"] = {
        "vmid": vmid, "hostname": c.get("hostname") or "#%s" % vmid,
        "status": c.get("status") or "", "state": st.get("state") or "none",
        "provisioned": st.get("provisioned") or 0, "declared": len(decl),
        # Le moteur ne provisionne QUE les slots de son budget bootté : déclarer plus de sorties que
        # `tx_slots` ne suffit pas (apply_layout ignore le surplus) → le front doit le DIRE.
        "tx_slots_len": len(tx_slots),
        # DÉCLARÉ (base) vs SERVI (env figé au `docker run`). Les confondre fait croire à l'exploitant
        # que ses sorties existent dès l'application, alors que le moteur tourne encore sur son ancien
        # budget : c'est `booted_active_tx` qui dit ce qui émet aujourd'hui.
        "active_tx_count": int(params.get("active_tx_count") or 0) or len(tx_slots),
        "booted_active_tx": _booted,
        "owned_slots": out.get("owned_slots") or [],
        "unit_kind": _unit["kind"], "unit_label": _unit["label"],
    }
    # Slots AFFICHÉS = les DÉCLARÉS (le modèle) + les slots RÉELLEMENT actifs hors modèle (marqués
    # `out_of_layout` par layout_status = une vraie anomalie à recaler). PAS la réserve structurelle
    # vide (tx_slots au-delà du modèle) : la montrer affichait N slots « hors modèle » fantômes (ex. 28
    # sur tx_count=32) et laissait croire à une capacité de 32 — or le vrai plafond = files RL de la carte.
    _orphans = sorted({int(k) for k, v in slot_states.items()
                       if v == "out_of_layout" and int(k) >= len(decl)})
    for i in list(range(len(decl))) + _orphans:
        d = decl[i] if i < len(decl) else None
        t = (tx_slots[i] or {}) if i < len(tx_slots) else {}
        state = slot_states.get(i) or slot_states.get(str(i)) or ("declared" if d else "out_of_layout")
        src = _slot_source(params, i)
        out["slots"].append({
            "idx": i,
            "declared": d,
            "queues": slot_queue_cost(d) if d else 0,
            "state": state,
            "source": src,
            "name": _slot_name(params, i),
            # Format RÉELLEMENT annoncé par la session (celui que le SDP publie) — peut différer du
            # layout tant qu'on n'a pas appliqué : c'est PRÉCISÉMENT ce qu'il faut voir.
            "announced": ({"w": t.get("width"), "h": t.get("height"), "fps": t.get("fps"),
                           "scan": t.get("scan") or "p", "bd": t.get("bit_depth") or 8}
                          if t.get("width") else None),
            "dest": ({"mcast": t.get("multicast_ip"), "port": t.get("dest_port")}
                     if t.get("multicast_ip") else None),
            "audios": len(t.get("audios") or []),
            "anc": bool(t.get("anc_multicast_ip")),
        })
    try:
        from . import tx_maintenance as _txm
        out["pending"] = _txm.list_pending(vmid)
    except Exception as e:
        log.warning("card_model %s: bac de maintenance: %s", vmid, e)
    return out


def draft_verdict(node_id, iface, slots):
    """Coût d'un layout BROUILLON, calculé AVANT le clic (« aucun contrôle muet ; un coût s'affiche
    AVANT l'action, pas après ») : rejoue `apply_layout` à blanc sur le brouillon et classe le
    résultat (`tx_maintenance.classify`). Retourne le verdict (level/victims/created/rl…), ou None
    s'il n'y a pas de moteur déployé sur cette carte (rien à perturber : la déclaration est gratuite
    tant qu'aucune session n'existe)."""
    import json as _json
    from . import tx_maintenance as _txm
    c = engine_for_card(node_id, iface)
    if not c:
        return None
    vmid = c["vmid"]
    try:
        params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        params = {}
    after = preview_layout_params(vmid, params, slots_decl=slots)
    v = _txm.classify(vmid, after, op="tx_layout_apply", params_before=params)
    v["vmid"] = vmid
    return v
