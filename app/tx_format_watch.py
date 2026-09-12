# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Surveillance du format des sources câblées aux sorties TX — étage 3 de docs/reference/TX_LAYOUTS.md.

## Le problème

Le gate de câblage (`cabling._tx_slot_mismatch`) garantit un INVARIANT au moment du geste :
**le format de la source câblée à un slot TX concorde avec le format déclaré du slot.** C'est cet
invariant qui rend l'activation d'une sortie gratuite (swap de source, zéro `rte_tm_hierarchy_commit`).

Mais une source peut changer de format EN EXPLOITATION (une caméra bascule 1080i50 → 1080p50, un
mur multiview est reconfiguré) : l'invariant se brise **sans geste humain**. La sortie TX annonce
alors dans son SDP un format qu'elle n'émet plus — non-conformité 2110, et piège pour l'équipement
d'en face.

## Ce qu'on fait (décision produit, dans CET ordre)

1. **ALERTER d'abord** — l'opérateur doit savoir AVANT que le système n'agisse (`warning`, texte
   explicite qui nomme la sortie, le format annoncé et le format reçu).
2. **Puis insérer un UDC** (réglage `tx_format_autoudc`, défaut True) qui reconvertit la source vers
   le format DÉCLARÉ du slot → l'invariant est rétabli.

C'est **sûr** au sens du chantier : insérer un UDC revient à changer la SOURCE du slot TX, et la
source n'est pas dans la signature de session (`mtl_rx.c:compute_sig`, patch bobi.studio) → swap,
**zéro commit, zéro stop de port**. La sortie continue d'émettre l'ancienne source pendant que le
conteneur UDC démarre.

Réglage coupé (`tx_format_autoudc=False`) ⇒ **l'alerte reste** : jamais de contrôle muet.

## Source de vérité

Le `flow_def` MXL du producteur (`cabling._flow_def_format`), pas le `deploy_config` — c'est
précisément le cas où la DB du producteur ment (une source qui a basculé sans redéploiement).
Poll lent (60 s par défaut) : un `host_exec` par sortie TX câblée, et seulement pour elles.
"""

import logging
from .numerotation import cle_tx_shm, cle_tx_audio_shm, cle_tx_anc_shm
import threading
import time

log = logging.getLogger(__name__)

_started = False
_signaled = {}      # (vmid, slot) → signature d'écart déjà alertée (cache RAM du chemin chaud)
# Le MÊME état, SURVIVANT au redémarrage (cf. app/episodes.py) : une dérive de format dure jusqu'à
# correction, la ré-annoncer à chaque boot est du bruit. Purgé des conteneurs disparus — un vmid
# recyclé qui hériterait d'une signature resterait SILENCIEUX sur sa propre dérive.
from .episodes import EtatEpisodes as _Episodes
_episodes = _Episodes("tx_format")


def purger_episodes():
    """Retire les signatures des conteneurs disparus (appelée par la boucle de ce module — elle
    était documentée « depuis surveillance » mais n'y a jamais été branchée : le fichier d'état
    grossissait, et un vmid recyclé restait MUET sur sa propre dérive)."""
    try:
        from .database import db_get_containers
        vivants = {str(c.get("vmid")) for c in (db_get_containers() or [])}
    except Exception:
        return
    _episodes.purger(lambda cle: cle.split("\x1f")[0] in vivants)


def _engines():
    """Moteurs 2110_io déployés → [(container, params)]."""
    import json
    from .database import db_get_containers
    out = []
    for c in db_get_containers():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") == "2110_io":
            out.append((c, dc.get("params") or {}))
    return out


def _reconfigure_udc(udc_vmid, want):
    """La source d'une sortie TX EST DÉJÀ un UDC dont la sortie a dérivé du format attendu : on le
    RECONFIGURE (à chaud + en DB) au lieu d'en empiler un second devant. Empiler cascaderait à
    l'infini (constaté au banc : udc → udc → udc), et chaque étage ajoute une trame de latence."""
    import json
    from .database import db_get_container, db_update_deploy_config, db_add_alert
    from .addressing import get_container_ip
    import requests as _req
    c = db_get_container(udc_vmid) or {}
    ip = c.get("ip") or get_container_ip(udc_vmid)
    body = {"width": want["width"], "height": want["height"], "fps": want["fps"],
            "scan": want["scan"], "field_order": want.get("field_order") or ""}
    if not ip:
        db_add_alert("alert.deploy.udc_ip_introuvable_reconfig", "error", vmid=udc_vmid, kind="deploy",
                     params={"vmid": udc_vmid})
        return
    try:
        _req.post("http://%s:8082/params" % ip, json=body, timeout=3)
    except Exception as e:
        db_add_alert("alert.deploy.udc_reconfig_echouee", "warning", vmid=udc_vmid, kind="deploy",
                     params={"vmid": udc_vmid, "e": e})
        return
    try:
        dc = json.loads(c.get("deploy_config") or "{}") or {}
        p = dict(dc.get("params") or {})
        p.update(body)
        db_update_deploy_config(udc_vmid, "udc", p)
    except Exception as e:
        log.warning("UDC #%s : persistance du format : %s", udc_vmid, e)
    db_add_alert("alert.deploy.udc_reconfigure_format", "info", vmid=udc_vmid, kind="deploy",
                 params={"vmid": udc_vmid, "width": want["width"], "height": want["height"],
                         "scan": want["scan"], "fps": want["fps"]})


def _container_type(vmid):
    import json
    from .database import db_get_container
    try:
        return (json.loads((db_get_container(vmid) or {}).get("deploy_config") or "{}") or {}).get("type")
    except Exception:
        return None


def _producer_of(shm):
    """vmid du producteur du flux `shm` (scan des `produces` de la flotte). None si introuvable."""
    import json
    from .database import db_get_containers
    from . import plugins as _pl
    for c in db_get_containers():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        t = dc.get("type")
        if not t or not _pl.is_plugin(t):
            continue
        hn = (dc.get("params") or {}).get("hostname") or c.get("hostname") or ""
        try:
            _ph = _pl.get_hook(t, "topology_ports")
            prods = (_ph(hn, dc.get("params") or {}, {}) or {}).get("produces") or [] if _ph \
                else _pl.derive_wiring(t, hn, dc.get("params") or {}).get("produces") or []
        except Exception:
            continue
        for p in prods:
            if p.get("shm") == shm:
                return c["vmid"]
    return None


def scan_once():
    """Un tour de surveillance. Retourne la liste des dérives détectées (dicts)."""
    from . import settings as _st, tx_maintenance as _txm
    from .database import db_add_alert
    from .routes.cabling import _flow_def_format, _insert_udc
    auto = _st.get("tx_format_autoudc", True)
    auto = (auto is True or str(auto).strip().lower() in ("1", "true", "yes", "on"))
    drifts, pending = [], []
    for c, params in _engines():
        vmid = c["vmid"]
        for i, _slot in enumerate(params.get("tx_slots") or []):
            shm = (params.get(cle_tx_shm(i)) or "").strip()
            if not shm:
                continue
            sf = _txm.slot_format(params, i)
            if not sf or not (sf.get("width") and sf.get("height")):
                continue
            src_vmid = _producer_of(shm)
            if not src_vmid:
                continue
            real = _flow_def_format(src_vmid, shm)
            if not real:
                continue                        # flux absent / illisible → ce n'est pas une dérive
            axes = _txm.format_diff(real, sf)
            key = (vmid, i)
            if not axes:
                _signaled.pop(key, None)
                continue
            sig = "|".join("%s=%s" % (a["axis"], a["source"]) for a in axes)
            if key not in _signaled:
                _signaled[key] = _episodes.get(key)     # reprise après (re)démarrage
            if _signaled.get(key) == sig:
                continue                        # déjà alerté pour CETTE dérive : on ne rabâche pas
            _signaled[key] = sig
            _episodes.poser(key, sig)
            label = _txm._slot_label(params, i)
            _fmt = lambda f: "%sx%s%s%s" % (f.get("width"), f.get("height"),
                                            f.get("scan") or "p", f.get("fps"))
            drifts.append({"vmid": vmid, "slot": i, "label": label, "shm": shm,
                           "src_vmid": src_vmid, "axes": axes})
            # 1) L'ALERTE D'ABORD : l'opérateur doit savoir avant que le système n'agisse.
            _cle = "alert.tx_stall.derive_format_auto" if auto else "alert.tx_stall.derive_format_manuel"
            db_add_alert(_cle, "warning", kind="tx_stall",
                        params={"h": c.get("hostname") or "#%s" % vmid, "label": label, "shm": shm,
                                "reel": _fmt(real), "annonce": _fmt(sf)})
            if not auto:
                continue
            # 2) PUIS l'insertion. Changer la source d'un slot TX n'est PAS dans compute_sig → swap,
            #    zéro commit TM. On préfère RÉUTILISER un UDC libre (pas de conteneur de plus).
            if _container_type(src_vmid) == "udc":
                _reconfigure_udc(src_vmid, sf)      # jamais d'UDC devant un UDC (cascade)
                continue
            pending.append((src_vmid, shm, vmid, i))
    if pending:
        # SÉQUENTIEL, dans UN thread : « UDC libre » = « sortie consommée par personne » — deux
        # insertions en parallèle éliraient le MÊME UDC libre (sa sortie n'est pas encore câblée)
        # et la seconde écraserait le format de la première. Constaté au banc.
        threading.Thread(target=_insert_all, args=(pending,), daemon=True, name="tx-fmt-udc").start()
    return drifts


def _insert_all(pending):
    """SÉQUENTIEL et en mode CREATE. Deux leçons du banc :
      · en parallèle, deux insertions élisent le MÊME « UDC libre » (sa sortie n'est pas encore
        câblée quand la seconde regarde) et la seconde écrase le format de la première ;
      · « UDC libre » = « sortie consommée par personne » = aussi un UDC que quelqu'un vient de
        déployer et n'a pas encore câblé. Une action AUTOMATIQUE ne doit jamais réquisitionner
        le conteneur d'un tiers (constaté au banc : le watcher a repris un UDC en cours de mise en
        service). La réutilisation reste offerte à l'humain, dans la modale."""
    from .routes.cabling import _insert_udc
    from .vmlocks import verrou_vmid
    for src_vmid, shm, vmid, i in pending:
        try:
            with verrou_vmid(vmid, op="tx-format-drift"):
                _insert_udc(src_vmid, shm, vmid, "video", i, "create", None, None)
        except Exception as e:
            log.error("insertion UDC (dérive de format TX #%s slot %s) : %s", vmid, i, e)


def start(interval=60):
    """Thread daemon de surveillance (idempotent). Lancé depuis main.py."""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        while True:
            try:
                purger_episodes()
                scan_once()
            except Exception as e:
                log.warning("tx_format_watch: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="tx-format-watch").start()
