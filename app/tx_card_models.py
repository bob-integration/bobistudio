# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Bibliothèque de MODÈLES de carte 2110 (gabarits réutilisables, par TYPE de carte).

★ Le chaînon manquant du chantier docs/reference/TX_LAYOUTS.md. Jusqu'ici, on éditait les sorties TX **directement
sur une interface** : aucun gabarit réutilisable n'existait. Le modèle produit se lit en DEUX temps :

  1. on RÈGLE des modèles par **TYPE de carte** (« E810-C 100G — 8× 1080p50 + 4× 1080i25 »). Un modèle
     est une **déclaration** : il ne touche aucun matériel, il ne coûte **RIEN** ;
  2. on APPLIQUE un modèle à une **carte réelle** (page Interfaces) : là, et là seulement, le coût se
     paie — en DPDK, provisionner les sorties recalcule l'arbre de pacing (arrêt du port entier). En
     **AF-XDP il n'y a pas de rate limiter → aucun commit → l'application est GRATUITE**.

⚠ Un modèle est une **SOURCE**. La **VÉRITÉ** reste le layout appliqué de la carte
(`io2110_layouts` : blob settings `tx_layout_<node>_<iface>`, puis `deploy_config.tx_slots` du
moteur). Une carte peut **diverger** du modèle dont elle est issue (édition experte, moteur
redéployé…) : ce module le DIT (`card_binding`), il ne le corrige pas en silence.

Validation : un modèle est validé contre les **capacités du TYPE** de carte (`nic_profiles` mesuré,
sinon la biblio statique `mtl.nic_rl_tx_cap`, sinon le plancher sûr) — un gabarit qui ne tient pas
dans la carte est REFUSÉ, en le disant. Le coût en files RL d'un slot est celui d'`io2110_layouts`
(1 vidéo + 1/audio + 1 si ANC) : une seule définition, pas deux.
"""

import logging

log = logging.getLogger(__name__)

_BIND_KEY = "tx_layout_binding_%d_%s"     # (node_id, iface) → {model_id, model_name, applied_at}


# ─── Types de carte (la bibliothèque de cartes existante) ─────────────────────────────────────────

def card_types():
    """Types de carte connus : profils MESURÉS (`nic_profiles`) + modèles réellement présents sur les
    nœuds (`node_interfaces.model` des cartes média). Chacun porte son plafond de files RL — c'est la
    contrainte DURE contre laquelle un modèle est validé."""
    from .database import db_all_nic_profiles, db_get_nodes, db_get_node_interfaces
    from . import mtl as _mtl
    seen = {}

    def _add(model, source, measured=0, rl_tx_cap=None, narrow_ok=None):
        model = (model or "").strip()
        if not model:
            return
        key = model.lower()
        cap = int(rl_tx_cap or 0) or _mtl.nic_rl_tx_cap(model)
        cur = seen.get(key)
        # Un profil mesuré prime sur une simple présence terrain (même règle que _node_rl_tx_cap).
        if cur and not (measured and not cur.get("measured")):
            cur["sources"] = sorted(set(cur["sources"] + [source]))
            return
        seen[key] = {"key": key, "model": model, "rl_tx_cap": cap,
                     "narrow_ok": (1 if narrow_ok is None else int(bool(narrow_ok))),
                     "measured": int(bool(measured)),
                     "speed_mbps": (cur or {}).get("speed_mbps", 0),
                     "sources": sorted(set((cur or {}).get("sources", []) + [source]))}

    for p in db_all_nic_profiles():
        _add(p.get("model"), "profile", measured=p.get("measured"),
             rl_tx_cap=p.get("rl_tx_cap"), narrow_ok=p.get("narrow_ok"))
    try:
        for n in db_get_nodes() or []:
            for r in db_get_node_interfaces(n["id"]) or []:
                if r.get("role") == "media2110":
                    _add(r.get("model"), "node")
                    # Vitesse de lien du TYPE = MAX des speed_mbps des cartes média dont le .model
                    # correspond (sous-chaîne insensible à la casse, comme compatible_models). La
                    # source de débit est node_interfaces (les nic_profiles n'ont pas de speed) → on
                    # attribue chaque interface à TOUS les types dont la clé matche son modèle.
                    ispeed = int(r.get("speed_mbps") or 0)
                    if ispeed > 0:
                        imodel = (r.get("model") or "").strip().lower()
                        for t in seen.values():
                            k = t["key"]
                            if imodel and (k in imodel or imodel in k):
                                t["speed_mbps"] = max(int(t.get("speed_mbps") or 0), ispeed)
    except Exception as e:                                   # base incomplète : la liste reste utile
        log.warning("card_types: interfaces nœuds: %s", e)
    return sorted(seen.values(), key=lambda t: t["model"].lower())


def type_caps(nic_model):
    """Capacités du TYPE de carte : plafond de files RL (profil mesuré > biblio statique > plancher)."""
    from . import mtl as _mtl
    for t in card_types():
        if t["key"] == (nic_model or "").strip().lower():
            return {"rl_tx_cap": t["rl_tx_cap"], "measured": t["measured"],
                    "narrow_ok": t["narrow_ok"], "known": True}
    return {"rl_tx_cap": _mtl.nic_rl_tx_cap(nic_model), "measured": 0, "narrow_ok": 1,
            "known": False}


# ─── Validation d'un modèle contre son type de carte ──────────────────────────────────────────────

def validate(nic_model, slots, observed=False):
    """Un gabarit qui ne tient pas dans la carte est REFUSÉ — en le disant (jamais de refus muet).
    La contrainte dure = les files du rate limiter (1 vidéo + 1/audio + 1 ANC par sortie).

    `observed=True` (CAPTURE d'une carte réelle) : le dépassement d'un plafond NON MESURÉ devient un
    AVERTISSEMENT, pas un refus. Le plafond d'un type non qualifié est un PLANCHER SÛR deviné
    (mtl.NIC_RL_TX_CAP_DEFAULT = 7) : refuser de capturer une carte qui fait DÉJÀ tourner 16 sorties
    au prétexte qu'on a deviné 7 serait absurde — la réalité prime sur l'estimation. Un plafond
    MESURÉ (qualification au banc), lui, reste une contrainte dure."""
    from . import io2110_layouts as _lay
    slots = _lay._normalize_slots(slots)
    caps = type_caps(nic_model)
    used = sum(_lay.slot_queue_cost(s) for s in slots)
    # Débit vidéo agrégé estimé (garde-fou de lien) : somme des débits par slot (0 pour un slot
    # audio/ANC-seul). L'entrelacé est déjà ramené à la moitié dans `_slot_bw_mbps`.
    bw_mbps = sum(_lay._slot_bw_mbps(s) for s in slots)
    errors, warnings = [], []
    if not (nic_model or "").strip():
        errors.append("type de carte requis : un modèle se règle POUR un type de carte "
                      "(c'est lui qui borne le nombre de sorties).")
    if used > caps["rl_tx_cap"]:
        msg = ("%d files TX requises > plafond du type (%d%s)"
               % (used, caps["rl_tx_cap"], "" if caps["measured"] else ", non mesuré"))
        if observed and not caps["measured"]:
            warnings.append(msg + " — plafond DEVINÉ (plancher sûr) : la carte fait déjà tourner ces "
                                  "sorties, on la capture telle quelle. Qualifier la carte fixera son "
                                  "vrai plafond.")
        else:
            errors.append(msg + " — retirer des sorties, des flux audio ou l'ANC.")
    if not slots:
        warnings.append("modèle vide : aucune sortie déclarée.")
    if not caps["known"] and (nic_model or "").strip():
        warnings.append("type de carte inconnu de la bibliothèque : plafond de files pris au plancher "
                        "sûr (%d). Qualifier la carte relèvera cette valeur." % caps["rl_tx_cap"])
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "used_queues": used, "bw_mbps": round(bw_mbps, 1), **caps}


# ─── CRUD (aucun effet matériel : déclarer un modèle ne coûte RIEN) ───────────────────────────────

def list_models():
    from .database import db_all_tx_card_models
    out = []
    for m in db_all_tx_card_models():
        v = validate(m.get("nic_model"), m.get("slots"))
        out.append({**m, "used_queues": v["used_queues"], "rl_tx_cap": v["rl_tx_cap"],
                    "bw_mbps": v["bw_mbps"], "valid": v["ok"], "errors": v["errors"],
                    "slot_count": len(m.get("slots") or [])})
    return out


def save_model(mid, name=None, nic_model=None, slots=None, notes=None, actor="", observed=False):
    """Crée (mid=None) ou met à jour un modèle. Refuse un gabarit qui dépasse le type (avec la raison).
    `observed=True` = capture d'une carte RÉELLE : un plafond non mesuré ne peut pas refuser ce qui
    tourne déjà (cf. validate). Retourne (ok, id|None, check)."""
    from .database import (db_create_tx_card_model, db_update_tx_card_model, db_get_tx_card_model)
    from . import io2110_layouts as _lay
    cur = db_get_tx_card_model(mid) if mid else None
    if mid and not cur:
        return False, None, {"ok": False, "errors": ["modèle introuvable"], "warnings": []}
    eff_model = nic_model if nic_model is not None else (cur or {}).get("nic_model") or ""
    eff_slots = slots if slots is not None else (cur or {}).get("slots") or []
    check = validate(eff_model, eff_slots, observed=observed)
    if not check["ok"]:
        return False, (mid or None), check
    norm = _lay._normalize_slots(eff_slots)
    if mid:
        db_update_tx_card_model(mid, actor=actor, name=name, nic_model=nic_model,
                                slots=(norm if slots is not None else None), notes=notes)
        return True, mid, check
    new_id = db_create_tx_card_model(name or "Sans nom", eff_model, norm, notes or "", actor)
    return True, new_id, check


def duplicate_model(mid, actor=""):
    from .database import db_get_tx_card_model, db_create_tx_card_model
    m = db_get_tx_card_model(mid)
    if not m:
        return None
    return db_create_tx_card_model("%s (copie)" % m["name"], m.get("nic_model") or "",
                                   m.get("slots") or [], m.get("notes") or "", actor)


# ─── Rattachement carte ↔ modèle (source vs vérité) ───────────────────────────────────────────────

def get_binding(node_id, iface):
    """Modèle dont le layout de cette carte est ISSU (dernière application), ou {}."""
    from .database import db_get_setting
    v = db_get_setting(_BIND_KEY % (int(node_id), iface), None)
    return v if isinstance(v, dict) else {}


def set_binding(node_id, iface, model):
    import time
    from .database import db_set_setting
    db_set_setting(_BIND_KEY % (int(node_id), iface),
                   {"model_id": model["id"], "model_name": model["name"],
                    "nic_model": model.get("nic_model") or "",
                    "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S")})


def _slots_equal(a, b):
    from . import io2110_layouts as _lay
    na, nb = _lay._normalize_slots(a), _lay._normalize_slots(b)
    if len(na) != len(nb):
        return False
    for x, y in zip(na, nb):
        if (x["video"] != y["video"] or x["audio_count"] != y["audio_count"]
                or bool(x["anc"]) != bool(y["anc"])):
            return False
    return True


def card_binding(node_id, iface):
    """État du rattachement d'une carte à un modèle, pour l'UI de la page Interfaces :
      · `model`      : le modèle source (ou None — layout édité à la main / jamais appliqué) ;
      · `diverged`   : le layout DÉCLARÉ de la carte diffère du modèle dont il est issu. Ce n'est PAS
                       une erreur (édition experte, modèle modifié depuis) — c'est une info qu'on DIT.
    """
    from .database import db_get_tx_card_model
    from . import io2110_layouts as _lay
    b = get_binding(node_id, iface)
    if not b.get("model_id"):
        return {"model": None, "diverged": False}
    m = db_get_tx_card_model(b["model_id"])
    if not m:                                   # modèle supprimé depuis : on garde la trace nominale
        return {"model": {"id": b["model_id"], "name": b.get("model_name") or "?", "deleted": True},
                "diverged": False, "applied_at": b.get("applied_at")}
    layout = _lay.get_layout(node_id, iface)
    return {"model": {"id": m["id"], "name": m["name"], "nic_model": m.get("nic_model") or ""},
            "applied_at": b.get("applied_at"),
            "diverged": not _slots_equal(layout.get("slots"), m.get("slots"))}


def compatible_models(node_id, iface):
    """Modèles applicables à cette carte : ceux dont le TYPE correspond au modèle de la carte. Les
    autres sont retournés aussi, marqués `compatible:false` + la raison — on ne les cache pas en
    silence (l'utilisateur doit comprendre pourquoi son modèle n'apparaît pas)."""
    from . import io2110_layouts as _lay
    budget = _lay.nic_budget(node_id, iface)
    card = (budget.get("model") or "").strip().lower()
    out = []
    for m in list_models():
        mm = (m.get("nic_model") or "").strip().lower()
        if not card:
            ok, why = True, "type de la carte inconnu (non qualifiée) — compatibilité non vérifiable"
        elif mm and (mm in card or card in mm):
            ok, why = True, ""
        else:
            ok, why = False, ("modèle réglé pour « %s », cette carte est une « %s »"
                              % (m.get("nic_model") or "?", budget.get("model") or "?"))
        out.append({**m, "compatible": ok, "why": why})
    return out


# ─── AMORÇAGE : capturer une carte EXISTANTE en modèle ────────────────────────────────────────────
# Le premier geste réel n'est pas de remplir un formulaire vide : les cartes sont DÉJÀ configurées.
# On capture donc l'existant. ⚠ C'est une LECTURE : capturer ne change RIEN sur la carte (aucun
# commit, aucun redéploiement) — l'UI le dit.

def effective_slots(node_id, iface):
    """Sorties RÉELLES de la carte, telles qu'on les capturerait : le layout DÉCLARÉ s'il existe,
    sinon l'état du moteur (format ANNONCÉ par chaque session + nombre de flux audio + ANC). Sans ça,
    une carte configurée « à l'ancienne » (tx_slots poussés, aucun layout déclaré) se capturerait
    VIDE — le pire des résultats : un modèle qui ment."""
    from . import io2110_layouts as _lay
    layout = _lay.get_layout(node_id, iface).get("slots") or []
    if layout:
        return _lay._normalize_slots(layout)
    out = []
    for s in (_lay.card_model(node_id, iface).get("slots") or []):
        if s.get("declared"):
            out.append(s["declared"])
            continue
        a = s.get("announced")
        if not a or not a.get("w"):
            continue                     # sortie ni déclarée ni annoncée : rien à capturer
        out.append({"video": {"w": a.get("w"), "h": a.get("h"), "fps": a.get("fps") or 25,
                              "bd": a.get("bd") or 10, "scan": a.get("scan") or "p"},
                    "audio_count": int(s.get("audios") or 0), "anc": bool(s.get("anc"))})
    return _lay._normalize_slots(out)


def cards_inventory():
    """Cartes média 2110 du cluster (nœud, interface, type, nombre de sorties capturables) — sert à
    l'état vide de la bibliothèque (« créer un modèle À PARTIR d'une carte »). LECTURE SEULE."""
    from .database import db_get_nodes, db_get_node_interfaces
    out = []
    for n in db_get_nodes() or []:
        for r in db_get_node_interfaces(n["id"]) or []:
            if r.get("role") != "media2110":
                continue
            try:
                slots = effective_slots(n["id"], r["ifname"])
            except Exception as e:
                log.warning("cards_inventory %s/%s: %s", n["id"], r.get("ifname"), e)
                slots = []
            out.append({"node_id": n["id"], "node_name": n.get("name") or n.get("host") or ("#%s" % n["id"]),
                        "iface": r["ifname"], "nic_model": r.get("model") or "",
                        "pmd": (r.get("pmd") or "af_xdp"), "outputs": len(slots)})
    return out


def capture_card(node_id, iface, name, actor=""):
    """Crée un modèle depuis les sorties RÉELLES d'une carte, pré-rattaché à son TYPE, et lie la carte
    au modèle ainsi né (→ la carte n'est plus « issue d'aucun modèle », la divergence devient
    mesurable). Ne touche PAS la carte : aucune écriture de layout, aucun push moteur."""
    from . import io2110_layouts as _lay
    slots = effective_slots(node_id, iface)
    if not slots:
        return False, ("cette carte n'a aucune sortie à capturer (ni layout déclaré, ni session "
                       "annoncée par le moteur)"), None
    nic_model = (_lay.nic_budget(node_id, iface).get("model") or "").strip()
    ok, mid, check = save_model(None, name=name or ("Capture %s" % iface), nic_model=nic_model,
                                slots=slots, notes="", actor=actor, observed=True)
    if not ok:
        return False, "; ".join(check.get("errors") or ["capture refusée"]), check
    from .database import db_get_tx_card_model
    set_binding(node_id, iface, db_get_tx_card_model(mid))
    return True, mid, check


def apply_preview(node_id, iface, model_id):
    """Ce que l'application d'un modèle FERAIT à cette carte, calculé AVANT le clic :
      · `diff`    : sorties ajoutées / retirées / reformatées vs le layout déclaré actuel ;
      · `check`   : le modèle tient-il dans CETTE carte (files RL, débit) ;
      · `verdict` : le coût réel sur le moteur déployé (sessions à créer, sorties VICTIMES nommées).
                    `null` = aucun moteur déployé → rien à geler. En AF-XDP le verdict est non
                    perturbateur (pas de rate limiter → aucun commit possible) : l'UI doit le DIRE.
    Ne persiste RIEN.
    """
    from .database import db_get_tx_card_model
    from . import io2110_layouts as _lay
    m = db_get_tx_card_model(model_id)
    if not m:
        return None
    slots = _lay._normalize_slots(m.get("slots"))
    cur = _lay.get_layout(node_id, iface).get("slots") or []
    diff = []
    for i in range(max(len(cur), len(slots))):
        a = cur[i] if i < len(cur) else None
        b = slots[i] if i < len(slots) else None
        if a is None:
            diff.append({"idx": i, "op": "add", "after": b})
        elif b is None:
            diff.append({"idx": i, "op": "remove", "before": a})
        elif not _slots_equal([a], [b]):
            diff.append({"idx": i, "op": "change", "before": a, "after": b})
        else:
            diff.append({"idx": i, "op": "same", "after": b})
    try:
        verdict = _lay.draft_verdict(node_id, iface, slots)
    except Exception as e:
        log.warning("apply_preview %s/%s: verdict: %s", node_id, iface, e)
        verdict = None
    return {"model": {"id": m["id"], "name": m["name"], "nic_model": m.get("nic_model") or ""},
            "slots": slots, "diff": diff, "verdict": verdict,
            "check": _lay.validate_slots(node_id, iface, slots),
            "budget": _lay.nic_budget(node_id, iface)}


def apply_model_to_card(node_id, iface, model_id, actor=""):
    """Écrit le layout DÉCLARÉ de la carte depuis le modèle et mémorise le rattachement.
    ⚠ Ne touche PAS le moteur : provisionner les sessions (= le coût, en DPDK) reste l'action
    explicite `/api/mtl/<vmid>/tx-layout/apply`, gatée par la fenêtre de maintenance (étage 2)."""
    from .database import db_get_tx_card_model
    from . import io2110_layouts as _lay
    m = db_get_tx_card_model(model_id)
    if not m:
        return False, "modèle introuvable"
    ok, check = _lay.set_layout(node_id, iface, m.get("slots") or [], actor=actor)
    if not ok:
        return False, "; ".join(check.get("errors") or ["budget de la carte dépassé"])
    set_binding(node_id, iface, m)
    return True, {"check": check, "model": {"id": m["id"], "name": m["name"]}}
