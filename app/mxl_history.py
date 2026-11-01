# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Profondeur des ring buffers MXL sur TOUT nœud qui porte le bus — point d'entrée unique.

★ LE CONSTAT (Z8, 2026-10-29). La profondeur (`mxl_history_ms`, fichier `/dev/shm/mxl/options.json`
reposé au boot par tmpfiles.d, cf. `mtl.ensure_mxl_history`) n'était posée QUE par la prép MTL d'un
nœud 2110. La Z8, nœud DeckLink, ne l'avait jamais reçue : `node_health` levait « profondeur MXL
absente » à chaque redémarrage de l'orchestrateur (8 fois le 29/10) sans que rien ne la répare. Le
défaut SDK (200 ms) égale notre défaut, ce qui le masquait ; toute autre valeur réglée ne se serait
appliquée sur AUCUN nœud non-2110.

Déclencheurs (tous passent par `sync_node`, qui MESURE avant d'agir) — décision du 2026-10-30 :
  · INSTALLATION du nœud (`routes/enrollment._enroll_finalize`, fin commune d'install-node.sh, de
    l'ISO et du PXE) → posée sur TOUT nœud, avant qu'il ne porte le moindre flux ;
  · changement du réglage `mxl_history_ms` (global ou override de nœud) → `propagate_async` ;
  · route manuelle `POST /api/mtl/mxl_history?node_id=` (bouton du Monitoring → Nœuds) : rattrapage
    des nœuds installés AVANT cette pose.
Ne réparent PLUS : le déploiement (la pose y était synchrone) ni la dérive constatée par
`node_health`, qui ALERTE seulement — une réparation silencieuse cachait l'installation incomplète.

⚠ Les flux DÉJÀ OUVERTS gardent la profondeur de leur création : rien n'est redéployé ici. Quand
la valeur posée diffère de celle sous laquelle les flux vivants ont été créés, l'exploitant est
prévenu (alerte + état publié dans la vue nœud).

Best-effort, jamais silencieux : tout échec est journalisé ET alerté."""
import logging
import threading
import time

from .database import db_add_alert, db_get_node, db_get_nodes

log = logging.getLogger(__name__)

_status = {}             # node_id → dernier état publié (vue nœud du Monitoring)
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(node_id):
    with _locks_guard:
        return _locks.setdefault(node_id, threading.Lock())


def _clamp(v):
    from .mtl import MXL_HISTORY_MS_DEFAULT
    from .settings import MXL_HISTORY_MS_MIN, MXL_HISTORY_MS_MAX
    try:
        v = int(v)
    except (TypeError, ValueError):
        return MXL_HISTORY_MS_DEFAULT
    return min(MXL_HISTORY_MS_MAX, max(MXL_HISTORY_MS_MIN, v))


def node_override_ms(node_id):
    """Override PAR NŒUD de `mxl_history_ms` (node_settings), ou None. L'override de nœud ne passe
    pas par `settings.set` : il est borné ici."""
    try:
        from .database import db_get_node_setting, _NODE_SETTING_SENTINEL
        ov = db_get_node_setting(node_id, "mxl_history_ms", _NODE_SETTING_SENTINEL)
    except Exception:                                                        # noqa: BLE001
        return None
    if ov is _NODE_SETTING_SENTINEL or ov in (None, ""):
        return None
    return _clamp(ov)


def want_ms(node_id):
    """Profondeur VOULUE pour ce nœud : override de nœud > réglage global > défaut."""
    from . import settings
    from .mtl import MXL_HISTORY_MS_DEFAULT
    return _clamp(settings.setting_for("mxl_history_ms", node_id) or MXL_HISTORY_MS_DEFAULT)


def status(node_id):
    """Dernier état connu (pour la vue nœud), ou None si jamais mesuré depuis le démarrage."""
    st = _status.get(node_id)
    return dict(st) if st else None


def note_observed(node_id, cur_ms, want, persistent):
    """La sonde de dérive de node_health publie ici ce qu'elle a vu (pas d'aller-retour en plus)."""
    prev = _status.get(node_id) or {}
    _status[node_id] = dict(prev, ts=time.time(), cur_ms=cur_ms, want_ms=want,
                            persistent=bool(persistent), domain=True)


def _publish(node_id, w, acted, ok, msg, before, reason):
    from .mtl import MXL_HISTORY_MS_DEFAULT
    prev = _status.get(node_id) or {}
    st = dict(prev, ts=time.time(), want_ms=w, last_reason=reason, last_ok=bool(ok),
              last_acted=bool(acted), last_msg=str(msg)[:200])
    if before is not None:
        st["domain"] = bool(before.get("domain")) or (acted and ok)
    if ok and (acted or (before or {}).get("domain")):
        st["cur_ms"], st["persistent"] = w, True
    elif before is not None:
        cur = before.get("cur_ns")
        st["cur_ms"] = cur // 1_000_000 if cur else None
        st["persistent"] = before.get("tmpf_ns") is not None
    # Flux vivants créés sous une AUTRE profondeur : ils la gardent jusqu'au redéploiement de leurs
    # producteurs. Valeur d'avant = fichier vivant, ou le défaut SDK s'il était absent.
    if acted and ok and before and before.get("flows"):
        old = (before["cur_ns"] // 1_000_000) if before.get("cur_ns") else MXL_HISTORY_MS_DEFAULT
        if old != w:
            st["stale_flows"] = {"flows": before["flows"], "old_ms": old, "ts": time.time()}
    _status[node_id] = st
    return st


def sync_node(node_id, reason, want=None, create=False, vmid=None):
    """Point d'entrée UNIQUE : mesure la profondeur MXL du nœud et la pose si elle s'écarte de
    l'intention (`want` ou `want_ms(node_id)`). Tout nœud, 2110 ou non. Sérialisé par nœud.

    `create` : poser même si le nœud n'a pas encore de domaine (déploiement, geste manuel).
    → (agi, ok, msg). Un échec est journalisé ET alerté ; jamais d'exception."""
    node = db_get_node(node_id) or {}
    if not node:
        return False, False, "node not found"
    w = _clamp(want) if want is not None else want_ms(node_id)
    name = node.get("name") or node.get("host") or node_id
    with _lock_for(node_id):
        try:
            from . import mtl
            acted, ok, msg, before = mtl.sync_mxl_history(node, w, create=create)
        except Exception as e:                                               # noqa: BLE001
            acted, ok, msg, before = True, False, "crash: %s" % e, None
        st = _publish(node_id, w, acted, ok, msg, before, reason)
        if ok:
            if acted:
                log.info("MXL depth on node %s set to %d ms (%s): %s", name, w, reason, msg)
                sf = st.get("stale_flows")
                if sf and sf.get("ts", 0) >= st["ts"] - 1:
                    _alert_stale_flows(node_id, name, w, sf, vmid)
            return acted, ok, msg
    log.warning("MXL depth on node %s (%d ms, %s) failed: %s", name, w, reason, msg)
    try:
        db_add_alert("alert.prep.mxl_profondeur_echec", "warning", node_id=node_id, vmid=vmid,
                     kind="prep", params={"n": name, "want_ms": w, "e": str(msg)[:200]})
    except Exception as e:                                                   # noqa: BLE001
        log.error("MXL depth failure on node %s could not be alerted: %s", name, e)
    return acted, ok, msg


def _alert_stale_flows(node_id, name, w, sf, vmid):
    log.warning("MXL depth on node %s now %d ms, but %d live flow(s) keep %d ms until their "
                "producers are redeployed", name, w, sf["flows"], sf["old_ms"])
    try:
        db_add_alert("alert.prep.mxl_profondeur_flux_anciens", "warning", node_id=node_id,
                     vmid=vmid, kind="prep",
                     params={"n": name, "want_ms": w, "flows": sf["flows"], "old_ms": sf["old_ms"]})
    except Exception as e:                                                   # noqa: BLE001
        log.error("MXL stale-flow notice on node %s could not be alerted: %s", name, e)


def propagate(node_ids=None, reason="setting"):
    """Propage la profondeur voulue aux nœuds (tous par défaut) qui portent le bus MXL. Un nœud
    sans domaine est mesuré puis laissé tel quel (installé avant la pose : bouton de rattrapage)."""
    ids = list(node_ids) if node_ids is not None else [n["id"] for n in (db_get_nodes() or [])]
    out = {}
    for nid in ids:
        out[nid] = sync_node(nid, reason)
    return out


def propagate_async(node_ids=None, reason="setting"):
    """`propagate` dans un fil de fond : un changement de réglage n'attend pas les nœuds."""
    t = threading.Thread(target=propagate, args=(node_ids, reason), daemon=True,
                         name="mxl-history-propagate")
    t.start()
    return t
