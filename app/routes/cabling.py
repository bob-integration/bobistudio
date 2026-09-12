# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Câblage en place depuis la home : le user clique source puis destination → POST direct ici.
Le serveur met à jour le deploy_config du consommateur (hot-wire :8082 si possible, sinon
redéploiement async), gère l'insertion automatique d'un UDC convertisseur au gating de format,
et la persistance des snapshots de câblage / vues de disposition (page Câbles, mode « Libre »).

_fetch_plugin_state est aussi consommé par app/routes/home_dashboard.py (import local, résolu à
l'exécution d'une requête — cf. sa docstring)."""

import json
import logging
import os
import threading
import time

from flask import jsonify, request

from . import bp
from .shared import _load_dc
from ..auth import require_login, require_perm
from ..database import (db_get_container, db_get_containers, db_add_alert,
                      db_cable_snapshot_save, db_cable_snapshots_list,
                      db_cable_snapshot_get, db_cable_snapshot_delete,
                      db_cable_layout_save, db_cable_layouts_list,
                      db_cable_layout_get, db_cable_layout_delete)
from ..deploy import deployer_script
from ..vmlocks import verrou_vmid

log = logging.getLogger(__name__)


def _plugins_is(t):
    from .. import plugins as _pl
    return _pl.is_plugin(t)

def _plugin_input(ip, essence, shm, slot=None, fmt=None):
    """POST :8082/input {essence, shm, slot?, format?} pour un plugin hot-wire. `slot` n'est
    inclus que pour les plugins multi-entrées (ex. split). `format` = descripteur du producteur
    (résolution/fps/chroma) — consommé par les plugins format-aware (ex. udc), ignoré sinon.
    Renvoie (ok, detail)."""
    import requests as _req
    body = {"essence": essence, "shm": shm or ""}
    if slot is not None:
        body["slot"] = slot
    if fmt:
        body["format"] = fmt
    try:
        r = _req.post(f"http://{ip}:8082/input", json=body, timeout=2)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} de {ip}:8082"
    return True, "ok"

def _flow_def_format(from_vmid, shm):
    """Format RÉEL d'un flux = lu du flow_def MXL sur le nœud du PRODUCTEUR (SOURCE DE VÉRITÉ), pas
    du deploy_config (qui peut dériver si le producteur n'a pas été redéployé → dims fausses
    propagées aux consommateurs). Retourne un descripteur compatible derive_wiring (width/height/
    chroma/bit_depth/scan/fps…) ou None. Réservé au CÂBLAGE (host_exec ponctuel), pas à l'affichage
    haute fréquence (cf. _shm_fmt). Le flow_def est écrit par le producteur à ses dims réelles."""
    if not from_vmid or not shm:
        return None
    try:
        import sys
        from ..database import db_get_container, db_get_node
        from .. import node_driver
        from .images import _repo_root
        prod = db_get_container(from_vmid)
        node = db_get_node(prod.get("node_id")) if (prod and prod.get("node_id")) else None
        if not node:
            return None
        st = os.path.join(_repo_root(), "script_templates")
        if st not in sys.path:
            sys.path.insert(0, st)
        import bobimxl
        uuid = bobimxl.flow_id(shm)
        rc, out, _ = node_driver.host_exec(
            node, "cat /dev/shm/mxl/%s.mxl-flow/flow_def.json 2>/dev/null" % uuid, timeout=8)
        s = (out or "").strip()
        if not s.startswith("{"):
            return None
        d = json.loads(s)
        comps = {c.get("name"): c for c in (d.get("components") or [])}
        y, cb = comps.get("Y") or {}, comps.get("Cb") or {}
        # Dims de TRAME d'abord (`frame_*`) : en ENTRELACÉ, seules elles font foi (un producteur
        # tiers peut déclarer ses composants à la hauteur de CHAMP → 540 pris pour du 1080).
        w = int(d.get("frame_width") or y.get("width") or 0)
        h = int(d.get("frame_height") or y.get("height") or 0)
        if not (w and h):
            return None
        cw, ch = int(cb.get("width") or w), int(cb.get("height") or h)
        chroma = "444" if (cw == w and ch == h) else "420" if (cw * 2 == w and ch * 2 == h) else "422"
        im = str(d.get("interlace_mode") or "progressive")
        gr = d.get("grain_rate") or {}
        num, den = gr.get("numerator"), gr.get("denominator") or 1
        bd = int(y.get("bit_depth") or 8)
        return {"width": w, "height": h, "chroma": chroma, "bit_depth": bd,
                "scan": "p" if im.startswith("progressive") else "i",
                "field_order": "tff" if "tff" in im else ("bff" if "bff" in im else ""),
                "fps_num": int(num) if num else None, "fps_den": int(den),
                "fps": (str(num) if den == 1 else "%s/%s" % (num, den)) if num else "",
                "colorimetry": str(d.get("colorspace") or "").replace("BT", "") or None,
                "pix_fmt": ("yuv%sp" % chroma) if bd <= 8 else ("yuv%sp10le" % chroma)}
    except Exception:
        return None


def _producer_format_ex(from_vmid, shm):
    """(format, mesuré) — descripteur de format du flux `shm` produit par `from_vmid`, ET la
    PROVENANCE de ce descripteur. Deux provenances, qui n'ont pas la même autorité :

      · `mesuré=True`  — lu du flow_def MXL réel sur le nœud du producteur (`_flow_def_format`) :
        le flux EXISTE et c'est bien ce format qu'il porte. SOURCE DE VÉRITÉ.
      · `mesuré=False` — le flux n'est pas encore créé (source pas encore fonctionnelle, moteur pas
        démarré, RX sans signal) ; on retombe sur le format DÉCLARÉ (wiring/deploy_config), qui est
        une INTENTION, pas un constat.

    L'appelant DOIT distinguer les deux : un écart mesuré est un fait (on refuse), un écart déclaré
    est une prédiction (on prévient et on laisse pré-câbler — cf. `_format_gate`)."""
    if not from_vmid or not shm:
        return None, False
    _truth = _flow_def_format(from_vmid, shm)
    if _truth:
        return _truth, True
    try:
        from ..database import db_get_container
        from .. import plugins as _pl
        prod = db_get_container(from_vmid)
        pdc = _load_dc(prod) if prod else None
        if not pdc or not pdc.get("type") or not _pl.is_plugin(pdc["type"]):
            return None, False
        w = _pl.derive_wiring(pdc["type"], (prod.get("hostname") or ""), pdc.get("params") or {})
        for p in w.get("produces") or []:
            if p.get("shm") == shm and p.get("format"):
                return p["format"], False
    except Exception:
        pass
    return None, False


def _producer_format(from_vmid, shm):
    """Descripteur de format du flux `shm` (mesuré si possible, déclaré sinon), sans la provenance.
    Injecté tel quel dans le consommateur au câblage. Cf. `_producer_format_ex` quand la provenance
    compte (gating)."""
    return _producer_format_ex(from_vmid, shm)[0]

def _fetch_plugin_state(ip, endpoint="/state"):
    """GET http://{ip}:8082<endpoint> → dict d'état live d'un plugin (shm câblés via
    state_field). {} en cas d'échec / IP indispo. Timeout court (poll Câbles)."""
    if not ip:
        return {}
    try:
        import requests as _req
        r = _req.get(f"http://{ip}:8082{endpoint}", timeout=0.5)
        if r.status_code == 200:
            return r.json() or {}
    except Exception:
        pass
    return {}

# ─── États live des plugins : « périmé pendant rafraîchissement » ────────────
# ★ POURQUOI CE CACHE. `_fetch_plugin_states` interroge chaque conteneur en HTTP.
# Même en parallèle, ça reste 24 ms sur `/api/home/summary` — le deuxième poste
# de la requête, mesuré. Or cette donnée n'a pas besoin d'être fraîche à la
# milliseconde : elle dit quel shm est câblé où, et ça ne change que sur un geste
# d'exploitation, alors que la page interroge toutes les 2 s.
#
# ⚠ ET SURTOUT : PAS D'ÉCHANTILLONNEUR PERPÉTUEL. Un thread de fond qui sonderait
# le parc en continu ferait payer le coût même quand personne ne regarde — c'est
# la forme de sondage sans contre-pression qui a déjà saturé ce contrôleur. Ici
# le rafraîchissement n'existe que TANT QUE des requêtes arrivent : on sert la
# valeur en cache immédiatement et on relance en tâche de fond si elle a dépassé
# l'âge tiède. Sans trafic, plus rien ne tourne.
#
# Trois âges, et le troisième est le garde-fou : au-delà de `_ETATS_MAX_S` la
# donnée est trop vieille pour être servie, on attend le fetch. Sans cette borne,
# une page rouverte après une heure afficherait un câblage d'il y a une heure —
# une valeur périmée mais plausible, exactement ce qu'on cherche à éviter.
_ETATS_TIEDE_S = 1.0     # au-delà : on sert le cache ET on rafraîchit derrière
_ETATS_MAX_S   = 10.0    # au-delà : trop vieux, on attend
_etats_cache = {"ts": 0.0, "cles": None, "val": {}}
_etats_lock = threading.Lock()
_etats_envol = [False]


def _fetch_plugin_states_cache(cibles, max_parallele=16):
    """`_fetch_plugin_states` avec cache tiède. Même contrat de retour."""
    if not cibles:
        return {}
    cles = tuple(sorted((c, ip, ep) for c, ip, ep in cibles))
    now = time.time()
    with _etats_lock:
        frais = (_etats_cache["cles"] == cles) and (now - _etats_cache["ts"])
        val = dict(_etats_cache["val"]) if _etats_cache["cles"] == cles else None
        envol = _etats_envol[0]

    # Cibles changées (câblage modifié) ou cache trop vieux → on attend.
    if val is None or frais is False or frais > _ETATS_MAX_S:
        val = _fetch_plugin_states(cibles, max_parallele)
        with _etats_lock:
            _etats_cache.update({"ts": time.time(), "cles": cles, "val": dict(val)})
        return val

    if frais > _ETATS_TIEDE_S and not envol:
        # Rafraîchissement DERRIÈRE la réponse. Un seul en vol : sans ce drapeau,
        # dix onglets déclencheraient dix rafraîchissements simultanés du parc.
        with _etats_lock:
            if _etats_envol[0]:
                return val
            _etats_envol[0] = True

        def _rafraichir():
            try:
                v = _fetch_plugin_states(cibles, max_parallele)
                with _etats_lock:
                    _etats_cache.update({"ts": time.time(), "cles": cles, "val": dict(v)})
            except Exception:                                            # noqa: BLE001
                log.debug("rafraîchissement des états de plugins échoué", exc_info=True)
            finally:
                with _etats_lock:
                    _etats_envol[0] = False

        threading.Thread(target=_rafraichir, name="etats-plugins", daemon=True).start()
    return val


def _fetch_plugin_states(cibles, max_parallele=16):
    """États live de PLUSIEURS plugins, en parallèle. `cibles` = [(clé, ip, endpoint)] →
    {clé: état}. Les échecs valent `{}`, comme `_fetch_plugin_state`.

    Séquentiellement, chaque appel vaut jusqu'à 0,5 s de timeout : douze conteneurs dont
    quelques-uns sont lents, c'est plusieurs secondes sur une route pollée toutes les 2 s.
    Mesuré le 2026-08-13 sur `/api/home/summary` : 579 ms des 706 ms de la requête partaient là,
    et le pire cas théorique (tous injoignables) valait 6 s — plus que l'intervalle de poll.
    En parallèle, le pire cas retombe au timeout d'UN seul appel.

    Le parallélisme est ici gratuit côté GIL : ces threads passent leur temps en attente réseau.
    """
    if not cibles:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=min(len(cibles), max_parallele)) as ex:
        futs = {ex.submit(_fetch_plugin_state, ip, ep): cle for cle, ip, ep in cibles}
        for f, cle in futs.items():
            try:
                out[cle] = f.result()
            except Exception:                                              # noqa: BLE001
                out[cle] = {}
    return out


def _fetch_mixer_inputs(ip, n):
    """GET http://{ip}:8082/state pour récupérer les shm câblés sur chaque input.
    Retourne [] en cas d'échec / IP indispo. Timeout court pour ne pas ralentir le poll."""
    if not ip or n <= 0:
        return []
    try:
        import requests as _req
        r = _req.get(f"http://{ip}:8082/state", timeout=0.5)
        if r.status_code == 200:
            return list((r.json() or {}).get("inputs") or [])
    except Exception:
        pass
    return []

def _mixer_input(ip, slot, shm):
    """POST http://{ip}:8082/input pour câbler/décâbler un slot du mixer.
    Renvoie (ok: bool, detail: str). detail décrit l'échec pour remonter au user."""
    import requests as _req
    try:
        r = _req.post(f"http://{ip}:8082/input",
                      json={"idx": int(slot), "shm": shm or ""},
                      timeout=2)
    except _req.exceptions.ConnectionError as e:
        return False, f"connexion refusée vers {ip}:8082 (mélangeur down ?) — {e}"
    except _req.exceptions.Timeout:
        return False, f"timeout (>2s) sur {ip}:8082"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        body = (r.text or "")[:200]
        return False, f"HTTP {r.status_code} de {ip}:8082 — {body}"
    return True, "ok"

def _hot_input(ip, body):
    """POST http://{ip}:8082/input générique (multiview/worker_udp/sender hot-input).
    Renvoie (ok, detail). Échoue silencieusement si :8082 absent (container non-hot)."""
    import requests as _req
    try:
        r = _req.post(f"http://{ip}:8082/input", json=body, timeout=2)
        return (r.status_code == 200), (r.text or "")[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def _try_hot_input(to_vmid, target, t, params, body, want_dims, cur_dims, adapts=False):
    """Hot-swap :8082/input si la résolution correspond et le container répond.
    Persiste params dans deploy_config SANS redéployer. Retourne (ok,status,payload)
    si géré à chaud, sinon None (→ l'appelant redéploie).

    `adapts` (plugin adapts_input : multiview/udc/delay) : le consommateur AUTO-DÉTECTE le format
    source au runtime (reader.format()) → le hot-swap est INCONDITIONNEL (pas d'exigence want==cur).
    Sans ça, câbler une source de dims inconnues/différentes (ex. PiP fraîchement ajouté, in_w=0)
    forçait un REDÉPLOIEMENT complet, qui recrée le flux de sortie et FIGE les consommateurs aval
    (obligeant à décâbler/recâbler la sortie). Cohérent avec _format_gate qui exempte adapts_input."""
    if not adapts and (want_dims is None or cur_dims is None or want_dims != cur_dims):
        return None
    from ..addressing import get_container_ip
    ip = target.get("ip") or get_container_ip(to_vmid)
    if not ip:
        return None
    ok, _detail = _hot_input(ip, body)
    if not ok:
        return None
    try:
        from ..database import db_update_deploy_config
        db_update_deploy_config(to_vmid, t, params)
    except Exception:
        pass
    return True, 200, {"to_vmid": to_vmid, "type": t, "hot_wired": True}

def _try_unwire_hot(to_vmid, target, t, params, body):
    """Détache une source à chaud via :8082/input {shm:""} (pas de contrainte de
    résolution). Persiste params sans redeploy. Retourne (ok,status,payload) ou None
    (→ l'appelant redéploie, ex. container non-hot sans :8082)."""
    from ..addressing import get_container_ip
    ip = target.get("ip") or get_container_ip(to_vmid)
    if not ip:
        return None
    ok, _d = _hot_input(ip, body)
    if not ok:
        return None
    try:
        from ..database import db_update_deploy_config
        db_update_deploy_config(to_vmid, t, params)
    except Exception:
        pass
    return True, 200, {"to_vmid": to_vmid, "type": t, "hot_wired": True}

def _format_gate(from_vmid, shm, to_type, params):
    """Gating broadcast au câblage. Retourne (verdict, raison) — verdict ∈ "ok" | "refuse" | "warn".

    Refuse une source vidéo incompatible avec le consommateur (NON-adaptateur) : RÉSOLUTION, CHROMA
    et CADENCE (num/den exact) doivent correspondre — sinon image illisible / judder. Adaptateurs
    (adapts_input : udc/multiview/delay) et consommateurs en auto-détection (largeur/hauteur non
    déclarées) sont EXEMPTÉS. La profondeur de bits reste un simple avertissement (réglage force8
    ambigu). Désactivable (wire_format_gating).

    PRÉ-CÂBLAGE (2026-08-05) : quand le format de la source est seulement DÉCLARÉ — le flux MXL
    n'existe pas encore (source pas démarrée, RX sans signal) — l'écart est une PRÉDICTION, pas un
    constat. Refuser interdisait de préparer un patch avant la mise en service, alors qu'un câble
    vers un consommateur logiciel est réversible et sans effet de bord matériel. On rend donc "warn"
    : le câble est posé, une alerte le dit, et `wire_format_watch` re-vérifie À L'APPARITION du flux
    (sans quoi on aurait troqué un refus explicite contre un échec silencieux). Les slots TX du
    moteur 2110, eux, gardent un refus SEC (gate séparé `_tx_slot_mismatch`, appelé côté endpoint) :
    là, un écart recrée la session et fige toutes les sorties de la carte."""
    from .. import settings as _st, plugins as _pl
    g = _st.get("wire_format_gating", True)
    if not (g is True or str(g).strip().lower() in ("1", "true", "yes", "on")):
        return "ok", None
    if (_pl.get(to_type) or {}).get("adapts_input"):
        return "ok", None                       # convertisseur : accepte n'importe quel format
    pf, _mesure = _producer_format_ex(from_vmid, shm)
    if not pf:
        return "ok", None                       # format producteur inconnu → ne bloque pas
    _ko = "refuse" if _mesure else "warn"
    cw = int(params.get("width") or params.get("out_width") or 0)
    ch = int(params.get("height") or params.get("out_height") or 0)
    pw = int(pf.get("width") or 0); ph = int(pf.get("height") or 0)
    if pw and ph and cw and ch and (pw, ph) != (cw, ch):
        return _ko, (f"résolution source {pw}×{ph} ≠ entrée {cw}×{ch} du {to_type} "
                     f"(image illisible) → insérer un UDC")
    pc, cc = str(pf.get("chroma") or ""), str(params.get("chroma") or "")
    if pc and cc and pc != cc:
        return _ko, f"chroma source {pc} ≠ {cc} attendu par le {to_type} → insérer un UDC"
    # Cadence : comparaison rationnelle EXACTE (num/den), gère le fractionnaire.
    pn, pd = pf.get("fps_num"), pf.get("fps_den")
    if not (pn and pd) and pf.get("fps"):
        pn, pd = _pl.rate_nd(pf.get("fps"))
    if pn and pd and params.get("fps"):
        cn, cd = _pl.rate_nd(params.get("fps"))
        if (pn, pd) != (cn, cd):
            _r = lambda n, d: n if d == 1 else round(n / d, 3)
            return _ko, (f"cadence source {_r(pn, pd)} ≠ référence {_r(cn, cd)} du {to_type} "
                         f"→ insérer une conversion UDC")
    return "ok", None


def _wire_format_pending(from_vmid, to_vmid, shm, to_type, why):
    """Inscrit un câble posé en PRÉ-CÂBLAGE pour re-vérification à l'apparition du flux
    (`app/wire_format_watch.py`). Best-effort : un échec est journalisé, jamais tu."""
    try:
        from .. import wire_format_watch as _wfw
        _wfw.inscrire(from_vmid, to_vmid, shm, to_type, why)
    except Exception as e:
        log.warning("pré-câblage %s → #%s : inscription impossible (%s)", shm, to_vmid, e)


def _wire_format_en_attente(to_vmid, shm):
    """L'entrée d'attente ({from_vmid,type,why}) si ce câble est un pré-câblage encore non vérifié,
    None sinon. Sert au retour d'API (l'UI doit annoncer un patch non tranché)."""
    if not shm:
        return None
    try:
        from .. import wire_format_watch as _wfw
        return _wfw._attente.get((int(to_vmid), shm))
    except Exception:
        return None


def _tx_slot_mismatch(from_vmid, shm, to_vmid, to_slot, kind="video"):
    """Étage 3 (docs/reference/TX_LAYOUTS.md) — GATE DE FORMAT SUR UN SLOT TX du moteur 2110_io.

    Une sortie TX pré-provisionnée (étage 1) annonce un format ; câbler une source d'un format
    DIFFÉRENT ne « convertit » rien : le format de la source est poussé dans la session
    (`controller.py:/input`), la SIGNATURE change (`mtl_rx.c:compute_sig`), la session est RECRÉÉE →
    `rte_tm_hierarchy_commit` → stop/start du PORT ENTIER (mesuré : +9 commits, ~1 s de gel sur
    TOUTES les sorties de la carte). C'est l'action la plus destructrice du système, et rien ne
    l'annonçait. On la refuse ; l'écart doit être RÉSOLU (UDC, ou alignement du slot), jamais forcé.

    Retourne None si tout concorde (ou si ça ne concerne pas un slot TX), sinon un dict :
        {engine, hostname, slot, label, axes:[{axis,source,slot}], source:{…}, slot_format:{…},
         chroma:{source,slot}?, verdict:{…}}   (`verdict` = classement étage 2 du câblage)"""
    if (kind or "video") != "video" or to_slot is None:
        return None
    from .. import settings as _st, tx_maintenance as _txm
    from ..database import db_get_container
    g = _st.get("tx_format_gating", True)
    if not (g is True or str(g).strip().lower() in ("1", "true", "yes", "on")):
        return None
    c = db_get_container(to_vmid)
    dc = _load_dc(c) if c else None
    if not dc or dc.get("type") != "2110_io":
        return None
    params = dc.get("params") or {}
    try:
        slot = int(to_slot)
    except (TypeError, ValueError):
        return None
    sf = _txm.slot_format(params, slot)
    if not sf or not (sf.get("width") and sf.get("height")):
        return None                      # slot sans format déclaré → rien à comparer
    pf = _producer_format(from_vmid, shm)
    if not pf:
        return None                      # format source inconnu → on ne crie pas dans le vide
    axes = _txm.format_diff(pf, sf)
    if not axes:
        return None
    out = {"engine": to_vmid, "hostname": (c.get("hostname") or "#%s" % to_vmid),
           "slot": slot, "label": _txm._slot_label(params, slot),
           "shm": shm, "from_vmid": from_vmid,
           "axes": axes, "source": pf,
           "slot_format": dict(sf, chroma=str(params.get("chroma") or "422"))}
    # CHROMA : n'entre NI dans compute_sig NI dans /input (la chroma du moteur est une constante
    # d'image) → aucun commit, donc PAS un motif de blocage. Mais un écart y rend l'image illisible :
    # on le signale dans la même modale au lieu de le taire.
    _pc, _cc = str(pf.get("chroma") or ""), str(params.get("chroma") or "422")
    if _pc and _cc and _pc != _cc:
        out["chroma"] = {"source": _pc, "engine": _cc}
    try:
        out["verdict"] = _txm.classify(
            to_vmid, _txm.preview(to_vmid, "tx_wire",
                                  {"slot": slot, "shm": shm, "kind": "video"}), op="tx_wire")
    except Exception as e:
        log.warning("gate format TX %s#%s : verdict incalculable : %s", to_vmid, slot, e)
    return out


def _is_sharded_assembler(vmid):
    """True si `vmid` est un multiview PARALLÉLISÉ par le tissu (assembleur avec ≥1 shard). Un tel
    multiview LIT les sorties de ses shards (pas les sources) à chaud → on ne doit JAMAIS le
    hot-inputer directement (ça débrancherait le shard de la tuile) : le câblage doit modifier la
    définition LOGIQUE (flux_config) puis re-réconcilier le tissu (re-planification des shards)."""
    import json as _json
    from ..database import db_fabric_all
    for row in db_fabric_all():
        if row.get("kind") in ("shard", "shared") and row.get("ref"):
            try:
                parents = _json.loads(row["parents"]) if row.get("parents") else []
            except Exception:
                parents = []
            if vmid in [int(p) for p in parents if str(p).isdigit()]:
                return True
    return False


def _apply_wire(from_vmid, to_vmid, shm, kind, to_slot=None, audio_shm=None, force=False):
    """Applique un câble. Retourne (ok, status, payload).

    `force` : passe outre le gate de format (`_format_gate`) — c'est le bouton « Câbler quand même »
    de la modale de résolution, où l'humain a VU les deux formats et les trois issues. L'écart reste
    tracé en alerte : forcer n'efface pas le fait. Sans effet sur le gate des slots TX 2110
    (`_tx_slot_mismatch`, côté endpoint), qui n'a pas de porte de sortie.

    Factorisé pour être appelé à la fois depuis l'endpoint /api/home/wire et
    depuis le restore de cable_snapshots qui boucle sur plusieurs ops.
    Lance le redéploiement async pour multiview/streamer/sender ;
    appelle l'agent HTTP en synchrone pour mixer/color_corrector (hot-wire)."""
    from ..database import db_get_container
    shm = (shm or "").strip()
    kind = kind or "video"
    if not to_vmid or not shm:
        return False, 400, {"error": "params manquants"}
    target = db_get_container(to_vmid)
    if not target:
        return False, 404, {"error": f"container #{to_vmid} introuvable"}
    dc = _load_dc(target)
    if not dc or not dc.get("type"):
        return False, 400, {"error": f"#{to_vmid} n'a pas de script déployé"}
    t      = dc["type"]
    params = dict(dc.get("params") or {})

    # Câblage INTER-NŒUD transparent : si le producteur et le consommateur sont sur des nœuds
    # différents, le flux n'existe pas dans le domaine MXL du consommateur → on provisionne
    # automatiquement la réplication RDMA (src_node → dst_node, même nom). Best-effort, dédup côté
    # service ; un échec (pas de NIC rdma) lève une alerte mais ne bloque pas le câble.
    try:
        from ..database import db_get_container as _dgc
        _prod = _dgc(from_vmid) if from_vmid else None
        _pn, _tn = (_prod or {}).get("node_id"), target.get("node_id")
        if _prod and _pn and _tn and int(_pn) != int(_tn):
            from services import rdma as _rdma
            for _fl, _fk in ((shm, kind or "video"), (audio_shm, "audio")):
                if not _fl:
                    continue
                _ok, _r = _rdma.ensure_cable_link(_pn, from_vmid, _fl, _tn, kind=_fk)
                if not _ok:
                    db_add_alert("alert.cablage.rdma_indisponible", "warning", vmid=to_vmid,
                                 kind="rx_stall", params={"shm": _fl, "vmid": to_vmid, "e": _r})
    except Exception as _e:
        log.warning("auto-RDMA câble %s → #%s : %s", shm, to_vmid, _e)

    # Gating broadcast : refuse une source incompatible (résolution/chroma/cadence) avec un
    # consommateur non-adaptateur, avec une raison (#27). Profondeur de bits = avertissement seul.
    if kind == "video":
        try:
            _verdict, _why = _format_gate(from_vmid, shm, t, params)
            if _verdict == "refuse" and force:
                db_add_alert("alert.cablage.force_ecart_format", "warning", vmid=to_vmid,
                             kind="deploy", params={"shm": shm, "vmid": to_vmid, "why": _why})
                _verdict = "ok"
            if _verdict == "refuse":
                db_add_alert("alert.cablage.refuse_ecart_format", "error", vmid=to_vmid,
                             kind="deploy", params={"shm": shm, "vmid": to_vmid, "why": _why})
                # Le refus doit PORTER SA RÉSOLUTION : les deux formats en cause, pour que l'UI
                # ouvre la modale « insérer un UDC / réutiliser / forcer » au lieu d'un message
                # d'erreur dans un coin. La détection côté client ne suffit pas — elle compare les
                # formats DÉCLARÉS de la topologie, alors que le refus se prononce sur le format
                # MESURÉ : quand les deux divergent (le cas exact d'un RX 2110 dont le moteur
                # annonce sa cadence globale), le client ne voit aucun écart et l'utilisateur
                # n'obtenait que le toast.
                return False, 409, {"error": _why, "measured": True,
                                    "format_conflict": {
                                        "why": _why,
                                        "source": _producer_format(from_vmid, shm),
                                        "target": _target_input_format(to_vmid, kind, to_slot),
                                        "from_vmid": from_vmid, "to_vmid": to_vmid,
                                        "shm": shm, "to_slot": to_slot}}
            if _verdict == "warn":
                # PRÉ-CÂBLAGE : le flux n'existe pas encore, l'écart est prédit sur le format
                # DÉCLARÉ. On pose le câble et on inscrit la vérification à faire à l'apparition du
                # flux — un câble « toléré » qu'on oublierait de re-contrôler serait exactement
                # l'échec silencieux que ce gate existe pour empêcher.
                db_add_alert("alert.cablage.pre_cablage_ecart", "warning", vmid=to_vmid,
                             kind="deploy", params={"shm": shm, "vmid": to_vmid, "why": _why})
                _wire_format_pending(from_vmid, to_vmid, shm, t, _why)
            _pf = _producer_format(from_vmid, shm)
            if _pf:
                _pbd = int(_pf.get("bit_depth") or 8)
                _cbd = int(params.get("bit_depth") or 8)
                if _pbd != _cbd:
                    db_add_alert("alert.cablage.profondeur_incoherente", "warning", vmid=to_vmid,
                                 kind="deploy",
                                 params={"shm": shm, "vmid": to_vmid, "pbd": _pbd, "cbd": _cbd})
        except Exception:
            pass

    from .. import plugins as _pl_wire
    _wire_hook = _pl_wire.get_hook(t, "wire_input")
    if _wire_hook:
        try:
            _wctx = {"vmid": to_vmid, "type": t,
                     "hostname": target.get("hostname") or "", "audio_shm": audio_shm}
            _wres = _wire_hook(kind, shm, to_slot, params, _wctx)
        except Exception as _e:
            return False, 400, {"error": str(_e)}
        if _wres is not None:
            params = _wres["params"]
            hot_idx = _wres.get("hot_idx")
            skip_hot = _wres.get("skip_hot", False)
            if not skip_hot:
                if kind == "video":
                    from ..monitor import _shm_dims
                    _tf = _flow_def_format(from_vmid, shm)   # source de vérité (flow_def), repli DB
                    want = (_tf["width"], _tf["height"]) if _tf else _shm_dims(shm)
                    v = params.get("video") or {}
                    cur = (int(v.get("width") or 0), int(v.get("height") or 0))
                    res = _try_hot_input(to_vmid, target, t, params,
                                        {"kind": "video", "shm": shm},
                                        want, cur if cur[0] else None)
                    if res:
                        return res
                else:
                    from ..addressing import get_container_ip
                    ip = target.get("ip") or get_container_ip(to_vmid)
                    if ip:
                        payload = {"kind": "audio", "shm": shm}
                        if hot_idx is not None:
                            payload["idx"] = hot_idx
                        ok, _d = _hot_input(ip, payload)
                        if ok:
                            try:
                                from ..database import db_update_deploy_config
                                db_update_deploy_config(to_vmid, t, params)
                            except Exception:
                                pass
                            return True, 200, {"to_vmid": to_vmid, "type": t, "hot_wired": True}
            def _async_wire():
                with verrou_vmid(to_vmid, op="wire"):
                    deployer_script(to_vmid, t, params)
            threading.Thread(target=_async_wire, daemon=True).start()
            return True, 200, {"to_vmid": to_vmid, "type": t}

    if _plugins_is(t):
        # Plugin câblable (manifest.wiring.mode == hot-wire) : POST :8082/input
        # {essence, shm, slot?}. Persiste le shm dans deploy_config (champ state_field).
        # Multi-entrées : la spec est choisie par (essence, slot) si un slot est fourni.
        from .. import plugins as _pl
        w = _pl.derive_wiring(t, target.get("hostname") or "", params)
        if w["mode"] != "hot-wire":
            return False, 400, {"error": f"{t} n'est pas câblable depuis la home (mode {w['mode']})"}
        cands = [x for x in w["consumes"] if (x.get("essence") or "video") == kind]
        if to_slot is not None:
            try: want_slot = int(to_slot)
            except (TypeError, ValueError): want_slot = None
            # Slot exact ; sinon repli sur une spec SANS slot (entrée unique non slottée,
            # ex. color_corrector : le front numérote le port à 0 alors que la spec n'a pas
            # de slot). Évite un faux « n'a pas d'entrée video #0 ».
            spec = (next((x for x in cands if x.get("slot") == want_slot), None)
                    or next((x for x in cands if x.get("slot") is None), None))
        else:
            spec = cands[0] if cands else None
        if not spec:
            return False, 400, {"error": f"{t} n'a pas d'entrée {kind}" + (f" #{(want_slot if want_slot is not None else 0) + 1}" if to_slot is not None else "")}
        slot = spec.get("slot")

        if spec.get("from_list"):
            # Entrée d'une liste à géométrie (ex. multiview flux_config). On édite l'entrée
            # existante (le slot doit exister — l'éditeur en ajoute), puis hot-input si la
            # résolution correspond, sinon redéploiement.
            lst_name = spec["from_list"]; sf = spec.get("shm_field", "shm"); pref = spec.get("shm_prefix", "")
            lst = list(params.get(lst_name) or [])
            if slot is None or not (0 <= slot < len(lst)):
                return False, 400, {"error": f"entrée #{(slot or 0) + 1} inexistante (ajoute-la via l'éditeur)"}
            newval = pref + shm
            # Pas de dédoublonnage : une même source peut alimenter plusieurs entrées
            # (ex. afficher le même flux dans plusieurs fenêtres multiview).
            entry = dict(lst[slot]); entry[sf] = newval
            nf = spec.get("name_field")
            if nf:
                ls = entry.get("label_source") or "hostname"
                if ls == "mxl_path":
                    entry[nf] = shm
                elif ls == "hostname":
                    producer = db_get_container(from_vmid) if from_vmid else None
                    entry[nf] = (producer or {}).get("hostname") or shm
            lst[slot] = entry; params[lst_name] = lst
            from ..monitor import _shm_dims
            _tf = _flow_def_format(from_vmid, shm)   # source de vérité (flow_def), repli DB
            want = (_tf["width"], _tf["height"]) if _tf else _shm_dims(shm)
            dims = spec.get("dims_fields") or []
            cur = None
            if len(dims) == 2:
                cur = (int(entry.get(dims[0]) or 0), int(entry.get(dims[1]) or 0))
                cur = cur if cur[0] else None
            ikey = spec.get("input_key", "idx")
            # Plugin adaptatif (multiview…) : hot-swap inconditionnel (auto-détecte le format).
            # On met à jour les dims stockées de l'entrée AVANT le hot pour que deploy_config et le
            # chip « format source » reflètent la nouvelle source sans redéploiement.
            _adapts = bool((_pl.get(t) or {}).get("adapts_input"))
            if _adapts and want and len(dims) == 2:
                entry[dims[0]], entry[dims[1]] = want
                lst[slot] = entry; params[lst_name] = lst
            # TISSU : si la cible est un ASSEMBLEUR shardé, NE PAS hot-inputer l'assembleur (il lit les
            # sorties de ses shards, pas les sources → un hot-input direct DÉBRANCHE le shard de la
            # tuile). On persiste le câblage LOGIQUE (flux_config) puis on RE-RÉCONCILIE le tissu : la
            # cellule change de signature → son shard est re-planifié sur la nouvelle source (via
            # pyramide) et l'assembleur reconfiguré sur la nouvelle sortie de shard. Même chemin que
            # l'éditeur de mur (cohérent). Cf. compositor_fabric.reconcile_fabric.
            if _is_sharded_assembler(to_vmid):
                try:
                    from ..database import db_update_deploy_config
                    db_update_deploy_config(to_vmid, t, params)
                except Exception:
                    pass
                _nid = target.get("node_id")
                def _async_fabric_wire():
                    from ..deploy import reconcile_fabric_node, reconcile_pyramide_sizes
                    with verrou_vmid(to_vmid, op="fabric-wire"):
                        try:
                            reconcile_fabric_node(_nid)
                            reconcile_pyramide_sizes(_nid)   # provisionne le proxy de la nouvelle source
                        except Exception as _e:
                            log.warning("reconcile tissu (câble %s slot %s): %s", to_vmid, slot, _e)
                threading.Thread(target=_async_fabric_wire, daemon=True).start()
                return True, 200, {"to_vmid": to_vmid, "type": t, "fabric_reconciled": True}
            # SUIVEURS des listes à géométrie (multiview) : câbler la VIDÉO d'une entrée fait
            # SUIVRE l'audio et l'ANC de la même entrée depuis les shm RÉELS produits par la
            # source (mêmes règles que wire_followers 2110_io : appariement par rang de vidéo,
            # groupage divisible, « toujours resuivre » — la source sans flux compagnon VIDE le
            # champ, le moteur retombe alors sur sa dérivation par nom). Un câble audio/ANC
            # direct ne déclenche AUCUN suiveur.
            _fl_bodies = []
            if kind == "video":
                _mates = [x for x in w["consumes"]
                          if x.get("from_list") == lst_name and x.get("slot") == slot
                          and (x.get("essence") or "video") in ("audio", "data")]
                if _mates:
                    _pp = []
                    try:
                        _pc = db_get_container(from_vmid) if from_vmid else None
                        _pdc = _load_dc(_pc) if _pc else None
                        if _pdc and _pl.is_plugin(_pdc.get("type")):
                            _pw = _pl.derive_wiring(_pdc["type"], _pc.get("hostname") or "",
                                                    _pdc.get("params") or {})
                            _pp = [{"essence": q.get("essence") or "video", "shm": q.get("shm")}
                                   for q in (_pw.get("produces") or []) if q.get("shm")]
                    except Exception:
                        _pp = []
                    _vids = [q["shm"] for q in _pp if q["essence"] == "video"]
                    _v = _vids.index(shm) if shm in _vids else 0
                    for _m in _mates:
                        _ess = _m.get("essence")
                        _flows = [q["shm"] for q in _pp
                                  if q["essence"] == ("audio" if _ess == "audio" else "data")]
                        _k = (len(_flows) // len(_vids)) if _vids and len(_flows) % len(_vids) == 0 else 0
                        _prog = _flows[_v * _k:(_v + 1) * _k] if _k else _flows
                        _fshm = _prog[0] if _prog else ""
                        _mf = _m.get("shm_field", "shm"); _mp = _m.get("shm_prefix", "")
                        entry[_mf] = (_mp + _fshm) if _fshm else ""
                        _fl_bodies.append({ikey: slot, "shm": _fshm, "essence": _ess})
                    lst[slot] = entry; params[lst_name] = lst
            res = _try_hot_input(to_vmid, target, t, params,
                                 {ikey: slot, "shm": shm, "essence": kind}, want, cur,
                                 adapts=_adapts)
            if res:
                # Câble principal appliqué à chaud (params persistés) → suiveurs à chaud aussi
                # (best-effort : au pire ils prendront effet au prochain redéploiement).
                if _fl_bodies:
                    from ..addressing import get_container_ip
                    _fip = target.get("ip") or get_container_ip(to_vmid)
                    for _b in _fl_bodies:
                        if _fip:
                            _hot_input(_fip, _b)
                return res
            if want and len(dims) == 2:
                entry[dims[0]], entry[dims[1]] = want
                lst[slot] = entry; params[lst_name] = lst
            # pas de return → redéploiement (tail _async_deploy)
        else:
            from ..addressing import get_container_ip
            ip = target.get("ip") or get_container_ip(to_vmid)
            if not ip:
                return False, 500, {"error": f"IP de {t} introuvable"}
            # Format du producteur injecté dans le consommateur (exploité par l'UDC).
            fmt = _producer_format(from_vmid, shm) if kind == "video" else None
            ok, detail = _plugin_input(ip, kind, shm, slot, fmt)
            if not ok:
                return False, 502, {"error": f"appel {t} : {detail}"}
            if spec.get("state_field"):
                params[spec["state_field"]] = shm
                if fmt:
                    # Stocke par state_field (ex. "input_v_0_fmt") pour les plugins
                    # multi-entrées, ET dans "input_format" pour les plugins mono-entrée
                    # passthrough (delay, avsync) qui n'ont pas width/height dans leurs params.
                    params[spec["state_field"] + "_fmt"] = fmt
                    params["input_format"] = fmt
                # Câbles « suiveurs » (ex. 2110_io : l'audio/ANC d'une sortie TX suit le câble vidéo)
                # — best-effort, appliqués à chaud + persistés avec le câble principal. On résout les
                # shm RÉELS produits par la SOURCE (produces du wiring) → le suiveur ne devine pas les
                # noms (le player produit p1_audio / p1_anc_0, pas p1_audio_0).
                _flw = _pl.get_hook(t, "wire_followers")
                if _flw:
                    _prod_produces = []
                    try:
                        _pc = db_get_container(from_vmid) if from_vmid else None
                        _pdc = _load_dc(_pc) if _pc else None
                        if _pdc and _pl.is_plugin(_pdc.get("type")):
                            _pw = _pl.derive_wiring(_pdc["type"], _pc.get("hostname") or "",
                                                    _pdc.get("params") or {})
                            _prod_produces = [{"essence": p.get("essence") or "video", "shm": p.get("shm")}
                                              for p in (_pw.get("produces") or []) if p.get("shm")]
                    except Exception:
                        _prod_produces = []
                    try:
                        _followers = _flw(kind, shm, slot, params,
                                          {"vmid": to_vmid, "type": t,
                                           "hostname": target.get("hostname") or "",
                                           "producer_produces": _prod_produces}) or []
                    except Exception:
                        _followers = []
                    for _f in _followers:
                        _plugin_input(ip, _f["essence"], _f.get("shm") or "", _f.get("slot"), None)
                        if _f.get("state_field"):
                            params[_f["state_field"]] = _f.get("shm") or ""
                try:
                    from ..database import db_update_deploy_config
                    db_update_deploy_config(to_vmid, t, params)
                except Exception:
                    pass
            return True, 200, {"to_vmid": to_vmid, "type": t, "hot_wired": True}

    else:
        return False, 400, {"error": f"type {t} n'est pas câblable depuis la home"}

    def _async_deploy():
        with verrou_vmid(to_vmid, op="wire-deploy"):
            deployer_script(to_vmid, t, params)
    threading.Thread(target=_async_deploy, daemon=True).start()
    return True, 200, {"to_vmid": to_vmid, "type": t}


@bp.route("/api/home/wire", methods=["POST"])
@require_perm("containers.deploy")
def api_home_wire():
    data = request.json or {}
    try:
        from_vmid = int(data.get("from_vmid") or 0)
        to_vmid   = int(data.get("to_vmid")   or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "vmids invalides"}), 400
    # Étage 2 (docs/reference/TX_LAYOUTS.md) : câbler une sortie 2110 est SÛR (swap de source, zéro commit) tant que
    # le format de la source CONCORDE avec le format provisionné du slot ; sinon la session est
    # recréée → `rte_tm_hierarchy_commit` → stop/start du port (MESURÉ au banc : +2 commits). On
    # calcule le verdict et on exige une confirmation NOMMANT les sorties qui vont figer. Gaté ICI
    # (geste humain) et pas dans `_apply_wire`, qui sert aussi aux restaurations/projets automatiques.
    # Étage 3 : ÉCART DE FORMAT sur un slot TX → refus SANS porte de sortie. Trois issues côté UI
    # (insérer un UDC / aligner le slot sur la source / annuler) — jamais « forcer ». Un TX qui
    # ANNONCE un format et en ÉMET un autre est une non-conformité 2110 ; et l'écart recrée la
    # session (commit TM = gel de toutes les sorties de la carte). Le gate est ici (geste humain),
    # pas dans `_apply_wire` (qui sert aussi aux restaurations, à l'insertion d'UDC et au watcher).
    try:
        _mm = _tx_slot_mismatch(from_vmid, (data.get("shm") or "").strip(),
                                to_vmid, data.get("to_slot"), data.get("kind") or "video")
    except Exception as _e:
        log.error("gate format TX %s→%s : %s", from_vmid, to_vmid, _e)
        _mm = None
    if _mm:
        _axes = ", ".join("%s %s ≠ %s" % (a["axis"], a["source"], a["slot"]) for a in _mm["axes"])
        db_add_alert("alert.cablage.format_refuse", "error", kind="deploy",
                     params={"shm": _mm["shm"], "label": _mm["label"], "hostname": _mm["hostname"],
                              "axes": _axes})
        return jsonify({"ok": False, "format_mismatch": _mm,
                        "error": "écart de format : %s" % _axes}), 409
    if data.get("to_slot") is not None and not data.get("confirm"):
        try:
            from ..database import db_get_container
            _c = db_get_container(to_vmid) or {}
            if '"2110_io"' in (_c.get("deploy_config") or ""):
                from .. import tx_maintenance as _txm
                _args = {"slot": int(data.get("to_slot")), "shm": data.get("shm"),
                         "kind": data.get("kind") or "video"}
                _v = _txm.classify(to_vmid, _txm.preview(to_vmid, "tx_wire", _args), op="tx_wire")
                if _v.get("level") == "disruptive":
                    return jsonify({"ok": False, "needs_confirm": True, "verdict": _v,
                                    "reason": _v.get("reason")}), 409
        except Exception as _e:
            log.warning("gate TX câblage %s→%s : %s", from_vmid, to_vmid, _e)
    ok, status, payload = _apply_wire(from_vmid, to_vmid,
                                       data.get("shm"), data.get("kind"),
                                       data.get("to_slot"), data.get("audio_shm"),
                                       force=bool(data.get("force")))
    if ok and data.get("anc_shm"):
        _apply_wire(from_vmid, to_vmid, data["anc_shm"], "data")
    # PRÉ-CÂBLAGE : le câble est posé mais l'écart de format prédit n'est pas tranché (le flux
    # n'existe pas encore). L'UI doit le dire — un succès muet ferait croire à un patch validé.
    if ok:
        _att = _wire_format_en_attente(to_vmid, (data.get("shm") or "").strip())
        if _att:
            payload = dict(payload, format_unverified=(_att.get("why") if isinstance(_att, dict) else True))
    return jsonify({"ok": ok, **payload}), status

def _target_input_format(to_vmid, kind="video", to_slot=None):
    """Format ATTENDU par le port d'entrée (kind/slot) du consommateur `to_vmid`, dérivé du
    wiring (consumes[].format). None si le consommateur auto-détecte (pas de format fixe)."""
    from ..database import db_get_container
    from .. import plugins as _pl
    c = db_get_container(to_vmid)
    dc = _load_dc(c) if c else None
    if not dc or not dc.get("type") or not _pl.is_plugin(dc["type"]):
        return None
    # Moteur 2110_io : le format attendu d'une sortie TX est celui DÉCLARÉ par le slot (tx_slots[i]),
    # pas un format de wiring (le manifeste n'en déclare aucun : un slot TX suit sa source). Sans ça,
    # l'UDC inséré devant un TX était créé au format PAR DÉFAUT (1280×720p25) — donc en écart avec le
    # slot, donc recréant la session : exactement ce que l'insertion doit éviter.
    if dc["type"] == "2110_io" and (kind or "video") == "video" and to_slot is not None:
        from .. import tx_maintenance as _txm
        sf = _txm.slot_format(dc.get("params") or {}, to_slot)
        if sf and sf.get("width") and sf.get("height"):
            sf = dict(sf)
            sf["chroma"] = str((dc.get("params") or {}).get("chroma") or "422")
            return sf
    w = _pl.derive_wiring(dc["type"], (c.get("hostname") or ""), dc.get("params") or {})
    cands = [x for x in w.get("consumes") or [] if (x.get("essence") or "video") == kind]
    spec = None
    if to_slot is not None:
        try: ws = int(to_slot)
        except (TypeError, ValueError): ws = None
        spec = (next((x for x in cands if x.get("slot") == ws), None)
                or next((x for x in cands if x.get("slot") is None), None))
    spec = spec or (cands[0] if cands else None)
    return (spec or {}).get("format")

def _udc_out_shm(vmid):
    """shm de sortie ({hostname}_udc) d'un container UDC déployé, via son wiring."""
    from ..database import db_get_container
    from .. import plugins as _pl
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    if not dc or dc.get("type") != "udc":
        return None
    w = _pl.derive_wiring("udc", (c.get("hostname") or ""), dc.get("params") or {})
    prods = [p.get("shm") for p in (w.get("produces") or []) if p.get("shm")]
    return prods[0] if prods else None

def _all_consumed_shms():
    """Ensemble des shm consommés (câblés en entrée) par TOUS les containers déployés."""
    from ..database import db_get_containers
    from .. import plugins as _pl
    shms = set()
    for c in db_get_containers():
        dc = _load_dc(c)
        if not dc:
            continue
        t = dc.get("type"); p = dc.get("params") or {}
        _cs_hook = _pl.get_hook(t, "consumed_shms") if t else None
        if _cs_hook:
            for s in (_cs_hook(p, {}) or []):
                if s: shms.add(s)
        elif _pl.is_plugin(t):
            try:
                w = _pl.derive_wiring(t, (c.get("hostname") or ""), p)
            except Exception:
                continue
            for spec in w.get("consumes") or []:
                shm = spec.get("shm") or (p.get(spec["state_field"]) if spec.get("state_field") else None)
                if shm: shms.add(shm)
    return shms

def _udc_is_free(vmid):
    """True si `vmid` est un UDC déployé dont la SORTIE n'est consommée par personne
    (réutilisable : on re-câblera son entrée ET sa sortie)."""
    out = _udc_out_shm(vmid)
    if not out:
        return False
    return out not in _all_consumed_shms()

def _insert_udc(from_vmid, shm, to_vmid, kind, to_slot, mode, reuse_vmid=None, node_id=None):
    """Insère un UDC convertisseur entre producteur et consommateur (formats différents).
    mode 'create' : crée+déploie un UDC ; mode 'reuse' : réutilise un UDC libre (repli create).
    Sortie de l'UDC = format attendu par la cible ; câble producteur→UDC→cible. Jalons via alertes.
    `udc` est docker-only (runtime compute) → la création passe par le chemin Docker compute
    sur un nœud (auto-pick si `node_id` absent), jamais par un vaisseau LXC."""
    from ..database import db_get_container, db_add_alert
    from ..deploy import deployer_script
    from ..addressing import get_container_ip
    from .. import docker_compute
    import requests as _req

    out_fmt = _target_input_format(to_vmid, kind, to_slot) or {}
    # BALAYAGE : l'UDC 0.9.0 sait sortir en ENTRELACÉ NATIF (les 4 combinaisons p→p, i→p, p→i, i→i).
    # On lui demande donc EXACTEMENT le format de la cible, ordre de champ et profondeur compris —
    # c'est ce qui rend l'insertion gratuite devant un slot TX (signature de session inchangée).
    # `fps` est passé en cadence TRAME : `udc._out_rate_nd` ne divise qu'au-dessus de 30, donc une
    # cadence trame (25/30) traverse intacte.
    _scan = "i" if str(out_fmt.get("scan") or "p").lower() == "i" else "p"
    _fps = float(out_fmt.get("fps") or 25) or 25
    udc_params = {
        "width": int(out_fmt.get("width") or 1280),
        "height": int(out_fmt.get("height") or 720),
        "fps": int(round(_fps)) if abs(_fps - round(_fps)) < 0.01 else _fps,
        "scan": _scan,
        "field_order": (str(out_fmt.get("field_order") or "tff").lower() if _scan == "i" else ""),
        "chroma": out_fmt.get("chroma") or "422",
        "input_shm": None, "input_format": None,
    }
    if out_fmt.get("bit_depth"):
        udc_params["bit_depth"] = int(out_fmt["bit_depth"])

    udc_vmid = None
    # La PROFONDEUR n'est pas reconfigurable à chaud (l'UDC lit BIT_DEPTH de son CONFIG au démarrage)
    # → un UDC libre dont la profondeur diffère de la cible ne peut pas la servir : on en crée un.
    if mode == "reuse" and reuse_vmid and udc_params.get("bit_depth"):
        _rc = db_get_container(int(reuse_vmid))
        _rdc = _load_dc(_rc) if _rc else None
        _rbd = int(((_rdc or {}).get("params") or {}).get("bit_depth") or 8)
        if _rbd != int(udc_params["bit_depth"]):
            db_add_alert("alert.cablage.udc_profondeur_incompatible", "info", vmid=udc_vmid,
                         kind="deploy",
                         params={"vmid": reuse_vmid, "rbd": _rbd, "cbd": udc_params["bit_depth"]})
            mode = "create"
    if mode == "reuse" and reuse_vmid and _udc_is_free(reuse_vmid):
        udc_vmid = int(reuse_vmid)
        c = db_get_container(udc_vmid)
        ip = c.get("ip") or get_container_ip(udc_vmid)
        if not ip:
            db_add_alert("alert.cablage.udc_ip_introuvable", "error", vmid=udc_vmid,
                         kind="deploy", params={"vmid": udc_vmid})
            return
        # Reconfigure la sortie à chaud (balayage compris — sinon un UDC libre repris devant un slot
        # entrelacé ressortait en progressif et l'écart persistait).
        try:
            _req.post(f"http://{ip}:8082/params", json={
                "width": udc_params["width"], "height": udc_params["height"],
                "fps": udc_params["fps"], "chroma": udc_params["chroma"],
                "scan": udc_params["scan"], "field_order": udc_params["field_order"]}, timeout=3)
        except Exception as e:
            db_add_alert("alert.cablage.udc_reconfig_echouee", "warning", vmid=udc_vmid,
                         kind="deploy", params={"vmid": udc_vmid, "e": e})
        # Le format vient de la DB (cf. monitor._shm_fmt) : un /params qui ne serait pas persisté
        # serait perdu au premier redéploiement → l'écart réapparaîtrait sans prévenir.
        try:
            from ..database import db_update_deploy_config
            _p = dict(((_load_dc(c) or {}).get("params")) or {})
            _p.update({k: v for k, v in udc_params.items()
                       if k in ("width", "height", "fps", "scan", "field_order", "chroma")})
            db_update_deploy_config(udc_vmid, "udc", _p)
        except Exception as e:
            log.warning("UDC #%s : persistance du format : %s", udc_vmid, e)
        db_add_alert("alert.cablage.udc_reutilise", "info", vmid=udc_vmid,
                     kind="deploy", params={"vmid": udc_vmid})
    else:
        # Création d'un nouvel UDC sur le chemin Docker compute (udc = docker-only).
        node_id = docker_compute.pick_compute_node(node_id)
        if not node_id:
            db_add_alert("alert.cablage.udc_aucun_noeud_compute", "error", kind="deploy")
            return
        # Le hostname dérive le shm de sortie ({hostname}_udc) → il doit être UNIQUE PAR CIBLE RÉELLE.
        # Sans le slot, deux sorties TX du MÊME moteur alimentées par la MÊME source (formats de slot
        # différents) créaient deux UDC de même hostname, donc DEUX ÉCRIVAINS SUR LE MÊME FLUX MXL.
        # Constaté au banc (moteur 140, slots TX#1 1080i25 et TX#3 720p50 tirés d'avsync).
        hostname = f"udc-{from_vmid}-{to_vmid}" + (f"-s{int(to_slot) + 1}" if to_slot is not None else "")
        db_add_alert("alert.cablage.udc_insertion_auto", "info", kind="deploy", params={"h": hostname})
        udc_vmid = docker_compute.creer_container_compute(node_id, "udc", hostname=hostname)
        if not udc_vmid:
            db_add_alert("alert.cablage.udc_creation_echouee", "error", kind="deploy")
            return
        # deployer_script route vers deploy_compute (docker run macvlan + attente agent :8081),
        # puis rend et POST le script.
        if not deployer_script(udc_vmid, "udc", udc_params):
            db_add_alert("alert.cablage.udc_deploiement_echoue", "error", vmid=udc_vmid,
                         kind="deploy", params={"vmid": udc_vmid})
            return
        ip = get_container_ip(udc_vmid)
        if not ip:
            db_add_alert("alert.cablage.udc_sans_ip", "error", vmid=udc_vmid,
                         kind="deploy", params={"vmid": udc_vmid})
            return
        # Attente que le contrôle :8082 réponde.
        ready = False
        for _ in range(20):
            try:
                if _req.get(f"http://{ip}:8082/state", timeout=2).status_code == 200:
                    ready = True; break
            except Exception:
                pass
            time.sleep(2)
        if not ready:
            db_add_alert("alert.cablage.udc_controle_non_pret", "warning", vmid=udc_vmid,
                         kind="deploy", params={"vmid": udc_vmid})

    # Câblage producteur → UDC (injecte input_format) puis UDC → destination.
    ok1, st1, p1 = _apply_wire(from_vmid, udc_vmid, shm, kind)
    if not ok1:
        db_add_alert("alert.cablage.udc_source_echouee", "error", vmid=udc_vmid,
                     kind="deploy", params={"vmid": udc_vmid, "e": p1.get('error')})
        return
    out_shm = _udc_out_shm(udc_vmid)
    if not out_shm:
        db_add_alert("alert.cablage.udc_shm_sortie_introuvable", "error", vmid=udc_vmid,
                     kind="deploy", params={"vmid": udc_vmid})
        return
    ok2, st2, p2 = _apply_wire(udc_vmid, to_vmid, out_shm, kind, to_slot)
    if not ok2:
        db_add_alert("alert.cablage.udc_destination_echouee", "error", vmid=udc_vmid,
                     kind="deploy", params={"vmid": udc_vmid, "e": p2.get('error')})
        return
    db_add_alert("alert.cablage.udc_insere", "info", vmid=udc_vmid, kind="deploy",
                 params={"vmid": udc_vmid, "from_vmid": from_vmid, "to_vmid": to_vmid,
                         "largeur": udc_params['width'], "hauteur": udc_params['height'],
                         "chroma": udc_params['chroma']})

@bp.route("/api/home/insert_udc", methods=["POST"])
@require_perm("containers.deploy")
def api_home_insert_udc():
    data = request.json or {}
    try:
        from_vmid = int(data.get("from_vmid") or 0)
        to_vmid   = int(data.get("to_vmid")   or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "vmids invalides"}), 400
    shm = (data.get("shm") or "").strip()
    if not from_vmid or not to_vmid or not shm:
        return jsonify({"ok": False, "error": "params manquants"}), 400
    mode = data.get("mode") if data.get("mode") in ("create", "reuse") else "create"
    reuse_vmid = data.get("reuse_vmid")
    kind = data.get("kind") or "video"
    to_slot = data.get("to_slot")
    node_id = data.get("node_id")   # optionnel : nœud compute cible (défaut = auto-pick)
    # Insertion UDC : reconfigure le CONSOMMATEUR `to_vmid` (source seulement lue ; l'UDC est
    # créé à la volée, vmid encore inconnu des autres opérations → pas de course dessus). On
    # sérialise donc sur `to_vmid`, l'entité partagée.
    def _insert_udc_locked():
        with verrou_vmid(to_vmid, op="insert-udc"):
            _insert_udc(from_vmid, shm, to_vmid, kind, to_slot, mode, reuse_vmid, node_id)
    threading.Thread(target=_insert_udc_locked, daemon=True).start()
    return jsonify({"ok": True, "status": "insertion_en_cours", "mode": mode})

def _flow_consumers_on_node(shm, node_id):
    """Nombre de consommateurs du flux `shm` sur le nœud `node_id`.

    Délègue à `plugins.ports_topologie` — **source unique** de « qui produit / qui consomme ce
    flux », partagée avec `services/rdma._cablage_flotte`. Voir là-bas pourquoi : deux réponses
    divergentes à cette question ont laissé une sortie 2110 de production sans image pendant des
    heures, la réconciliation créant le lien RDMA que la purge supprimait en boucle.

    Sert au teardown auto des liens RDMA inter-nœud : 0 → plus besoin de répliquer."""
    if not shm or node_id is None:
        return 0
    from ..database import db_get_containers
    from .. import plugins as _pl
    n = 0
    for c in db_get_containers():
        if c.get("node_id") != node_id:
            continue
        dc = _load_dc(c)
        if not dc or not dc.get("type"):
            continue
        ports = _pl.ports_topologie(dc["type"], c.get("hostname") or "", dc.get("params") or {})
        n += sum(1 for cons in ports["consumes"] if cons.get("shm") == shm)
    return n


def decable_flow_on_node(shm, node_id):
    """Décâble TOUS les consommateurs du flux `shm` sur le nœud `node_id` (via derive_wiring().consumes,
    shm résolu). Retourne le nombre de câbles effectivement retirés. Utilisé par la suppression EN
    CASCADE d'un lien RDMA depuis la page RDMA : retirer le lien retire d'abord les câbles qui en
    dépendent (sinon flux répliqué orphelin = échec silencieux). Le lien lui-même est supprimé par
    l'appelant (services.rdma.arreter_replication) — ce helper ne touche qu'aux câbles."""
    if not shm or node_id is None:
        return 0
    from ..database import db_get_containers
    from .. import plugins as _pl
    victims = []
    for c in db_get_containers():
        if c.get("node_id") != node_id:
            continue
        dc = _load_dc(c)
        if not dc or not dc.get("type") or not _pl.is_plugin(dc["type"]):
            continue
        try:
            w = _pl.derive_wiring(dc["type"], c.get("hostname") or "", dc.get("params") or {})
        except Exception:
            continue
        for cons in (w.get("consumes") or []):
            if cons.get("shm") == shm:
                victims.append((c.get("vmid"), cons.get("essence") or "video"))
    n = 0
    for vmid, essence in victims:
        try:
            ok, _st, _p = _apply_unwire(vmid, shm, essence)
            if ok:
                n += 1
        except Exception as _e:
            log.warning("cascade décâble %s @ #%s : %s", shm, vmid, _e)
    return n


def _maybe_release_rdma(to_vmid, shm, kind):
    """Après un décâblage : si le flux `shm` n'a plus de consommateur sur le nœud du consommateur,
    libère le lien RDMA auto inter-nœud correspondant. Best-effort."""
    try:
        from ..database import db_get_container
        c = db_get_container(to_vmid)
        nid = (c or {}).get("node_id")
        if nid is None:
            return
        from services import rdma as _rdma
        _rdma.release_cable_link(shm, nid, still_consumed=_flow_consumers_on_node(shm, nid) > 0)
    except Exception as _e:
        log.warning("release RDMA après décâble %s @ #%s : %s", shm, to_vmid, _e)


def _apply_unwire(to_vmid, shm, kind):
    """Supprime un câble. Retourne (ok, status, payload). Factorisé."""
    from ..database import db_get_container
    shm = (shm or "").strip()
    kind = kind or "video"
    if not to_vmid or not shm:
        return False, 400, {"error": "params manquants"}
    target = db_get_container(to_vmid)
    if not target:
        return False, 404, {"error": f"container #{to_vmid} introuvable"}

    dc = _load_dc(target)
    if not dc or not dc.get("type"):
        return False, 400, {"error": f"#{to_vmid} n'a pas de script déployé"}

    t      = dc["type"]
    params = dict(dc.get("params") or {})

    from .. import plugins as _pl_unwire
    _unwire_hook = _pl_unwire.get_hook(t, "unwire_input")
    if _unwire_hook:
        try:
            _uctx = {"vmid": to_vmid, "type": t, "hostname": target.get("hostname") or ""}
            _ures = _unwire_hook(kind, shm, None, params, _uctx)
        except Exception as _e:
            return False, 400, {"error": str(_e)}
        if _ures is not None:
            params = _ures["params"]
            hot_idx = _ures.get("hot_idx")
            if kind == "video":
                res = _try_unwire_hot(to_vmid, target, t, params, {"kind": "video", "shm": ""})
                if res:
                    return res
            elif hot_idx is not None:
                res = _try_unwire_hot(to_vmid, target, t, params,
                                      {"kind": "audio", "idx": hot_idx, "shm": ""})
                if res:
                    return res
            def _async_unwire():
                with verrou_vmid(to_vmid, op="unwire"):
                    deployer_script(to_vmid, t, params)
            threading.Thread(target=_async_unwire, daemon=True).start()
            return True, 200, {"to_vmid": to_vmid, "type": t}

    if _plugins_is(t):
        from .. import plugins as _pl
        w = _pl.derive_wiring(t, target.get("hostname") or "", params)
        cands = [x for x in w["consumes"] if (x.get("essence") or "video") == kind]
        # On décâble le slot portant ce shm (from_list : shm résolu ; state_field : params).
        spec = next((x for x in cands if x.get("from_list") and x.get("shm") == shm), None) \
               or next((x for x in cands if x.get("state_field") and params.get(x["state_field"]) == shm), None) \
               or (cands[0] if cands else None)
        if not spec:
            return False, 400, {"error": f"{t} n'a pas d'entrée {kind}"}
        slot = spec.get("slot")
        ikey = spec.get("input_key", "idx")

        if spec.get("from_list"):
            lst_name = spec["from_list"]; sf = spec.get("shm_field", "shm")
            lst = list(params.get(lst_name) or [])
            if slot is not None and 0 <= slot < len(lst):
                entry = dict(lst[slot]); entry[sf] = ""; lst[slot] = entry; params[lst_name] = lst
            # TISSU (symétrique du câblage) : assembleur shardé → modifier la définition LOGIQUE puis
            # re-réconcilier, jamais hot-unwirer l'assembleur (qui lit les sorties de shards).
            if _is_sharded_assembler(to_vmid):
                try:
                    from ..database import db_update_deploy_config
                    db_update_deploy_config(to_vmid, t, params)
                except Exception:
                    pass
                _nid = target.get("node_id")
                def _async_fabric_unwire():
                    from ..deploy import reconcile_fabric_node, reconcile_pyramide_sizes
                    with verrou_vmid(to_vmid, op="fabric-unwire"):
                        try:
                            reconcile_fabric_node(_nid)
                            reconcile_pyramide_sizes(_nid)
                        except Exception as _e:
                            log.warning("reconcile tissu (décâble %s slot %s): %s", to_vmid, slot, _e)
                threading.Thread(target=_async_fabric_unwire, daemon=True).start()
                return True, 200, {"to_vmid": to_vmid, "type": t, "fabric_reconciled": True}
            res = _try_unwire_hot(to_vmid, target, t, params, {ikey: slot, "shm": ""})
            if res:
                return res
            # pas de return → redéploiement (tail _async_deploy)
        else:
            from ..addressing import get_container_ip
            ip = target.get("ip") or get_container_ip(to_vmid)
            if not ip:
                return False, 500, {"error": f"IP de {t} introuvable"}
            ok, detail = _plugin_input(ip, kind, "", slot)
            if not ok:
                return False, 502, {"error": f"appel {t} : {detail}"}
            if spec.get("state_field"):
                params[spec["state_field"]] = ""
                # Décâblage vidéo → les câbles « suiveurs » (audio/ANC dérivés) sont vidés aussi.
                _flw = _pl.get_hook(t, "wire_followers")
                if _flw:
                    try:
                        _followers = _flw(kind, "", slot, params,
                                          {"vmid": to_vmid, "type": t,
                                           "hostname": target.get("hostname") or ""}) or []
                    except Exception:
                        _followers = []
                    for _f in _followers:
                        _plugin_input(ip, _f["essence"], "", _f.get("slot"), None)
                        if _f.get("state_field"):
                            params[_f["state_field"]] = ""
                try:
                    from ..database import db_update_deploy_config
                    db_update_deploy_config(to_vmid, t, params)
                except Exception:
                    pass
            return True, 200, {"to_vmid": to_vmid, "type": t, "hot_wired": True}

    else:
        return False, 400, {"error": f"type {t} non géré"}

    def _async_deploy():
        with verrou_vmid(to_vmid, op="wire-deploy"):
            deployer_script(to_vmid, t, params)
    threading.Thread(target=_async_deploy, daemon=True).start()
    return True, 200, {"to_vmid": to_vmid, "type": t}

@bp.route("/api/home/unwire", methods=["POST"])
@require_perm("containers.deploy")
def api_home_unwire():
    data = request.json or {}
    try:
        to_vmid = int(data.get("to_vmid") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "vmid invalide"}), 400
    ok, status, payload = _apply_unwire(to_vmid, data.get("shm"), data.get("kind"))
    if ok:                                      # teardown auto du lien RDMA inter-nœud si plus consommé
        _maybe_release_rdma(to_vmid, (data.get("shm") or "").strip(), data.get("kind"))
    return jsonify({"ok": ok, **payload}), status

def _collect_current_edges():
    """Reconstruit la liste des câbles actifs depuis les deploy_config + état
    live (mixer/corrector via HTTP). Format snapshot : list de dicts
    {from_vmid, to_vmid, shm, kind, to_slot?}. to_slot rempli pour multiview/mixer."""
    from .. import plugins as _pl
    containers = db_get_containers()
    producers = {}   # shm → vmid (premier producteur trouvé)
    consumers = []   # liste de {to_vmid, shm, kind, to_slot?}
    for c in containers:
        dc = _load_dc(c)
        if not dc:
            continue
        t = dc.get("type")
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        # Produces
        _ps_hook = _pl.get_hook(t, "produced_shms") if t else None
        if _ps_hook:
            for s in (_ps_hook(hn, p, {}) or []):
                producers.setdefault(s, c["vmid"])
        elif _pl.is_plugin(t):
            for prod in _pl.derive_wiring(t, hn, p)["produces"]:
                if prod.get("shm"):
                    producers.setdefault(prod["shm"], c["vmid"])
        # Consumes
        _tp2_hook = _pl.get_hook(t, "topology_ports") if t else None
        if _tp2_hook:
            for port in (_tp2_hook(hn, p, {}) or {}).get("consumes") or []:
                if port.get("shm"):
                    consumers.append({"to_vmid": c["vmid"], "shm": port["shm"],
                                      "kind": port.get("kind") or "video"})
        elif _pl.is_plugin(t):
            hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
            w = _pl.derive_wiring(t, hn, p)
            cons = w["consumes"]
            live = _fetch_plugin_state(c.get("ip"), w.get("state_endpoint") or "/state") \
                   if any(x.get("state_field") for x in cons) else {}
            for spec in cons:
                ess = spec.get("essence") or "video"
                shm = spec.get("shm") or (live.get(spec["state_field"]) if spec.get("state_field") else None)
                if shm:
                    cn = {"to_vmid": c["vmid"], "shm": shm, "kind": ess}
                    if spec.get("slot") is not None:
                        cn["to_slot"] = spec["slot"]
                    consumers.append(cn)
    edges = []
    for cn in consumers:
        from_vmid = producers.get(cn["shm"])
        if from_vmid is None:
            continue   # consommateur orphelin (producteur introuvable) : skip
        e = {"from_vmid": from_vmid, "to_vmid": cn["to_vmid"],
             "shm": cn["shm"], "kind": cn["kind"]}
        if "to_slot" in cn:
            e["to_slot"] = cn["to_slot"]
        edges.append(e)
    return edges

@bp.route("/api/cables/snapshots", methods=["GET"])
@require_login
def api_cable_snapshots_list():
    snaps = db_cable_snapshots_list()
    return jsonify({"snapshots": [
        {"id": s["id"], "name": s["name"], "created_at": s["created_at"],
         "edge_count": len((s["payload"] or {}).get("edges") or [])}
        for s in snaps
    ]})

@bp.route("/api/cables/snapshots", methods=["POST"])
@require_perm("containers.deploy")
def api_cable_snapshot_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "nom requis"}), 400
    edges = _collect_current_edges()
    sid = db_cable_snapshot_save(name, edges)
    return jsonify({"ok": True, "id": sid, "edge_count": len(edges)})

@bp.route("/api/cables/snapshots/<int:sid>", methods=["DELETE"])
@require_perm("containers.deploy")
def api_cable_snapshot_delete(sid):
    ok = db_cable_snapshot_delete(sid)
    return jsonify({"ok": ok})

@bp.route("/api/cables/clear", methods=["POST"])
@require_perm("containers.deploy")
def api_cables_clear():
    """Décâble tout le pipeline (boucle d'unwire sur l'état courant)."""
    edges = _collect_current_edges()
    results = []
    for e in edges:
        ok, status, payload = _apply_unwire(e["to_vmid"], e["shm"], e["kind"])
        results.append({"edge": e, "ok": ok, "status": status, "error": payload.get("error")})
    fails = [r for r in results if not r["ok"]]
    return jsonify({"ok": not fails, "cleared": len(edges) - len(fails),
                    "failed": len(fails), "errors": [r["error"] for r in fails if r.get("error")]})

@bp.route("/api/cables/snapshots/<int:sid>/restore", methods=["POST"])
@require_perm("containers.deploy")
def api_cable_snapshot_restore(sid):
    snap = db_cable_snapshot_get(sid)
    if not snap:
        return jsonify({"ok": False, "error": "snapshot introuvable"}), 404
    saved_edges = (snap.get("payload") or {}).get("edges") or []
    # 1) Décâble tout l'existant pour partir d'une page blanche
    current = _collect_current_edges()
    errors_clear = []
    for e in current:
        ok, _, payload = _apply_unwire(e["to_vmid"], e["shm"], e["kind"])
        if not ok and payload.get("error"):
            errors_clear.append(payload["error"])
    # 2) Recâble selon le snapshot
    errors_wire = []
    applied = 0
    for e in saved_edges:
        ok, _, payload = _apply_wire(e.get("from_vmid"), e.get("to_vmid"),
                                      e.get("shm"), e.get("kind"),
                                      e.get("to_slot"))
        if ok:
            applied += 1
        elif payload.get("error"):
            errors_wire.append(f"{e.get('shm')}: {payload['error']}")
    return jsonify({"ok": not errors_wire, "applied": applied,
                    "total": len(saved_edges), "errors": errors_wire,
                    "clear_errors": errors_clear})

# ─── Vues de DISPOSITION de la page Câbles (mode « Libre ») ───────────────────
# Ne touchent JAMAIS au câblage (≠ snapshots) : on stocke uniquement les positions des
# cartes et l'état replié, partagés entre utilisateurs.

@bp.route("/api/cables/layouts", methods=["GET"])
@require_login
def api_cable_layouts_list():
    return jsonify({"layouts": [
        {"id": l["id"], "name": l["name"], "created_at": l["created_at"],
         "node_count": len((l["payload"] or {}).get("positions") or {})}
        for l in db_cable_layouts_list()
    ]})

@bp.route("/api/cables/layouts", methods=["POST"])
@require_login
def api_cable_layout_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "nom requis"}), 400
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "payload invalide"}), 400
    payload = {"positions": payload.get("positions") or {},
               "collapsed": payload.get("collapsed") or []}
    lid = db_cable_layout_save(name, payload)
    return jsonify({"ok": True, "id": lid,
                    "node_count": len(payload["positions"])})

@bp.route("/api/cables/layouts/<int:lid>", methods=["GET"])
@require_login
def api_cable_layout_get(lid):
    layout = db_cable_layout_get(lid)
    if not layout:
        return jsonify({"ok": False, "error": "vue introuvable"}), 404
    return jsonify({"ok": True, "id": layout["id"], "name": layout["name"],
                    "payload": layout["payload"]})

@bp.route("/api/cables/layouts/<int:lid>", methods=["DELETE"])
@require_login
def api_cable_layout_delete(lid):
    return jsonify({"ok": db_cable_layout_delete(lid)})
