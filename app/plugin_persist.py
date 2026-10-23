"""Persistance GÉNÉRIQUE et DÉCLARATIVE de l'état qu'un plugin modifie à chaud.

Le proxy de plugin relayait sans rien retenir : un réglage posé à chaud (fader, mute, structure
d'une console) vivait dans le conteneur seul, et le premier redémarrage — auto-relance de la
surveillance, redéploiement, migration — le défaisait en silence. Seul le multiview avait un
code bespoke (`_persist_multiview_hot`). Ce module donne la même garantie à tout plugin qui la
DÉCLARE, sans que l'orchestrateur connaisse le plugin (cf. docs/chantiers/CONSOLE_AUDIO.md § 7.1) :

    "control": { …, "persist": {
        "param":     "console",                 # clé de deploy_config.params où vit l'état
        "snapshot":  "/doc",                    # GET (read_endpoints) : l'état COMPLET à retenir
        "settings":  ["/set", "/mute"],         # POST qui changent un réglage → instantané par lot
        "structure": {"apply": "/structure/apply", "validate": "/structure/validate"}
    }}

- RÉGLAGE : le conteneur applique et répond ; on relit l'instantané PAR LOT (un mouvement de
  fader produit des dizaines de POST par seconde, seule la dernière valeur compte) — premier
  écrit DEBOUNCE_S après le dernier geste, au plus tard MAX_WAIT_S après le premier ;
- STRUCTURE : validation à blanc par le conteneur → le document CANONIQUE qu'il renvoie est
  persisté → l'application est demandée. « Persister avant d'appliquer » : un conteneur qui
  meurt entre les deux redémarre sur le nouveau document. Application refusée (version
  périmée, saut de latence non accepté) → l'ancien document est remis.

L'écriture relit la config FRAÎCHE sous `verrou_vmid(op="config")` : jamais l'instantané de la
requête, qui écraserait un câblage ou un réglage concurrent.

Appelé par TOUS les chemins qui postent à un plugin : le proxy (`plugin_proxy`) et les macros
(action, POST avancé, feuille de param_tree) — un réglage posé par un pupitre doit survivre
autant qu'un réglage posé à la souris.
"""
import json
import threading
import time

import requests

DEBOUNCE_S = 1.0
MAX_WAIT_S = 5.0
HTTP_TIMEOUT_S = 5.0

_lock = threading.Lock()
_pending = {}          # vmid -> {"first": t0, "timer": Timer}
_stats = {"flushes": 0, "writes": 0, "errors": 0, "last_error": None}


def spec(m):
    """Déclaration `control.persist` normalisée, ou None si le plugin n'en déclare pas (ou une
    invalide : `validate_spec` dit pourquoi, le scan la refuse)."""
    p = ((m or {}).get("control") or {}).get("persist")
    if not isinstance(p, dict) or validate_spec(m):
        return None
    st = p.get("structure") or {}
    return {"param": p["param"], "snapshot": p["snapshot"],
            "settings": list(p.get("settings") or []),
            "apply": st.get("apply"), "validate": st.get("validate")}


def validate_spec(m):
    """Erreurs de la déclaration `control.persist` (liste vide = valide ou absente)."""
    ctrl = (m or {}).get("control") or {}
    p = ctrl.get("persist")
    if p is None:
        return []
    if not isinstance(p, dict):
        return ["control.persist doit être un objet"]
    errs = []
    eps = ctrl.get("endpoints") or []
    eps = set(eps) if isinstance(eps, list) else {("/" + k) for k in eps}
    reads = set(ctrl.get("read_endpoints") or [])
    if not isinstance(p.get("param"), str) or not p.get("param"):
        errs.append("control.persist.param : nom de clé de params requis")
    if p.get("snapshot") not in reads:
        errs.append("control.persist.snapshot doit être un des read_endpoints")
    for e in p.get("settings") or []:
        if e not in eps:
            errs.append("control.persist.settings : %s n'est pas dans control.endpoints" % e)
    st = p.get("structure")
    if st is not None:
        if not isinstance(st, dict) or st.get("apply") not in eps or st.get("validate") not in eps:
            errs.append("control.persist.structure : apply et validate doivent être dans control.endpoints")
    return errs


def _dc(vmid):
    from .database import db_get_container
    c = db_get_container(vmid)
    if not c:
        return None
    dc = c.get("deploy_config")
    try:
        return json.loads(dc) if isinstance(dc, str) else (dc or {})
    except ValueError:
        return {}


def _write(vmid, key, value, only_if=None):
    """Écrit params[key] = value, config relue fraîche sous verrou. `only_if` : n'écrit que si
    la valeur actuelle est celle-là (restauration après un refus, sans écraser un tiers).
    Renvoie True si la base a changé."""
    from .database import db_update_deploy_config
    from .vmlocks import verrou_vmid
    with verrou_vmid(vmid, op="config"):
        dc = _dc(vmid)
        if not dc or not dc.get("type"):
            return False
        params = dict(dc.get("params") or {})
        if only_if is not None and params.get(key) != only_if:
            return False
        if params.get(key) == value:
            return False
        params[key] = value
        db_update_deploy_config(vmid, dc["type"], params)
        _stats["writes"] += 1
        _refresh_script(vmid, dc["type"], params)
        # Libellés LIÉS (control.persist.labels) : le nom des éléments remplit un niveau de la page
        # Libellés — à chaque écriture du document, quel que soit le chemin (structure, réglage,
        # câblage). Ne lève jamais (cf. app/label_sync.py).
        from . import label_sync
        label_sync.push(vmid, dc["type"], params)
        return True


def _refresh_script(vmid, type_, params):
    """Réécrit le script SUR DISQUE dans le conteneur, rendu avec les params persistés — sans le
    relancer. Sans ça, la base avait raison mais le conteneur non : un redémarrage du script
    (auto-relance de la surveillance, `docker start` d'un conteneur compute, bouton Démarrer)
    relance le script déposé au dernier DÉPLOIEMENT, dont la config est figée dans le texte —
    mesuré sur le banc le 2026-10-13 : fader persisté à −9,5 dB, revenu à 0 dB après stop/start.
    Appelé sous le verrou du vmid (sérialisé avec un déploiement). Échec : alerte, jamais levé."""
    try:
        from .database import db_get_container
        from .scripts import generer_script
        from . import deploy as _d
        from .addressing import get_container_ip
        ip = get_container_ip(vmid)
        if not ip:
            raise RuntimeError("IP du conteneur introuvable")
        content = generer_script(type_, _d._gras(params), params.get("plugin_version") or None)
        if not content:
            raise RuntimeError("rendu du script vide")
        path = (db_get_container(vmid) or {}).get("script") or "/opt/script/main.py"
        r = _d.agent_session().post(_d.agent_url(ip, "/deploy"), json={"path": path, "content": content},
                                    headers=_d.agent_headers(vmid), timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError("agent /deploy → HTTP %s" % r.status_code)
        _stats["scripts"] = _stats.get("scripts", 0) + 1
    except Exception as e:
        _stats["errors"] += 1
        _stats["last_error"] = "#%s script: %s: %s" % (vmid, type(e).__name__, e)
        try:
            from .database import db_add_alert
            db_add_alert("#%s : état enregistré en base mais script non rafraîchi dans le conteneur (%s) — "
                         "un redémarrage du script reviendrait à l'état du dernier déploiement" % (vmid, e),
                         "warning", vmid=vmid)
        except Exception:
            pass


def _url(vmid, path):
    from .addressing import get_container_ip
    ip = get_container_ip(vmid)
    return ("http://%s:8082%s" % (ip, path)) if ip else None


# ── Réglages : instantané par lot ────────────────────────────────────────────────────
def after_post(vmid, m, endpoint, status):
    """À appeler après CHAQUE POST relayé à un plugin. Ne fait rien si le plugin ne déclare pas
    ce endpoint comme réglage persistant, ou si le conteneur a refusé (status ≥ 300)."""
    sp = spec(m)
    if not sp or not (200 <= int(status or 0) < 300) or endpoint not in sp["settings"]:
        return
    now = time.monotonic()
    with _lock:
        cur = _pending.get(vmid)
        if cur:
            cur["timer"].cancel()
            delay = DEBOUNCE_S if now - cur["first"] + DEBOUNCE_S <= MAX_WAIT_S else max(0.0, MAX_WAIT_S - (now - cur["first"]))
            first = cur["first"]
        else:
            delay, first = DEBOUNCE_S, now
        t = threading.Timer(delay, flush, args=(vmid,))
        t.daemon = True
        _pending[vmid] = {"first": first, "timer": t, "spec": sp}
        t.start()


def flush(vmid):
    """Relit l'instantané du conteneur et le persiste. Appelé par le minuteur (ou un test)."""
    with _lock:
        cur = _pending.pop(vmid, None)
    sp = (cur or {}).get("spec")
    if not sp:
        return False
    _stats["flushes"] += 1
    try:
        url = _url(vmid, sp["snapshot"])
        if not url:
            raise RuntimeError("IP du conteneur introuvable")
        r = requests.get(url, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError("%s → HTTP %s" % (sp["snapshot"], r.status_code))
        doc = r.json()
        if not isinstance(doc, dict):
            raise RuntimeError("%s : objet JSON attendu" % sp["snapshot"])
        return _write(vmid, sp["param"], doc)
    except Exception as e:                       # jamais remonté au geste de l'opérateur…
        _stats["errors"] += 1
        _stats["last_error"] = "#%s %s: %s" % (vmid, type(e).__name__, e)
        try:                                     # … mais jamais silencieux non plus
            from .database import db_add_alert
            db_add_alert("Réglages de #%s non enregistrés : %s" % (vmid, e), "warning", vmid=vmid)
        except Exception:
            pass
        return False


# ── Après un déploiement : persister la forme CANONIQUE ──────────────────────────────
AFTER_DEPLOY_DELAYS_S = (5.0, 15.0, 45.0)


def after_deploy(vmid, type_, delays=AFTER_DEPLOY_DELAYS_S):
    """Le conteneur démarre sur le document persisté et le met en forme canonique (défauts, ids,
    MIGRATIONS d'une nouvelle version du plugin). Tant que personne ne modifiait rien, la base
    gardait l'ancienne forme : la page Câbles et l'arbre des macros, qui lisent la base, montraient
    l'ancien câblage (mesuré 2026-10-15 sur la console 0.4.11). On relit donc l'instantané une fois
    le script démarré (quelques essais, le temps qu'il serve). Ne lève jamais ; sans déclaration
    `control.persist`, ne fait rien."""
    from . import plugins
    sp = spec(plugins.get(type_) or {})
    if not sp:
        return False
    t = threading.Thread(target=_after_deploy_run, args=(vmid, sp, tuple(delays)), daemon=True)
    t.start()
    return t


def _after_deploy_run(vmid, sp, delays):
    last = None
    for d in delays:
        time.sleep(d)
        try:
            url = _url(vmid, sp["snapshot"])
            if not url:
                raise RuntimeError("IP du conteneur introuvable")
            r = requests.get(url, timeout=HTTP_TIMEOUT_S)
            if r.status_code != 200:
                raise RuntimeError("%s → HTTP %s" % (sp["snapshot"], r.status_code))
            snap = r.json()
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            continue
        old = (_dc(vmid) or {}).get("params", {}).get(sp["param"])
        # ★ Un conteneur qui a REFUSÉ son document sert un objet d'erreur sur le même chemin : le
        # persister effacerait l'état en base. Seule une mise en forme du MÊME document est retenue
        # (même version, aucun CONTENU perdu) ; sinon la base reste telle quelle — rien n'est perdu.
        # Une migration peut abandonner une clé SIMPLE (console 0.4.16 : `listen_mode` passe sur chaque
        # monitoring), jamais une liste ou un objet (tranches, bus…).
        lost = set(old) - set(snap) if isinstance(snap, dict) and isinstance(old, dict) else set()
        if not isinstance(snap, dict) or not isinstance(old, dict) or snap.get("version") != old.get("version") \
                or any(isinstance(old[k], (list, dict)) for k in lost):
            _stats["last_error"] = "#%s après déploiement : instantané non retenu (autre document)" % vmid
            return False
        return _write(vmid, sp["param"], snap, only_if=old)
    _stats["last_error"] = "#%s après déploiement : instantané illisible (%s)" % (vmid, last)
    return False


def pending(vmid=None):
    with _lock:
        return (vmid in _pending) if vmid is not None else sorted(_pending)


def stats():
    return dict(_stats, pending=pending())


# ── Structure : persister avant d'appliquer ──────────────────────────────────────────
def structure_apply(vmid, m, endpoint, body, actor=None):
    """Si `endpoint` est l'application de structure déclarée : validation à blanc → persistance
    du document canonique → application → (refus : ancien document remis). Renvoie la réponse
    Flask (texte, statut, en-têtes), ou None si le endpoint n'est pas concerné.

    `actor` : auteur pour le journal d'exploitation (app/ops_journal.py) ; None = déduit du
    contexte de requête (proxy, page Câbles, page Libellés), « automatique » hors requête. C'est
    ICI que le diff est possible : l'ancien document est relu en base AVANT d'être remplacé."""
    sp = spec(m)
    if not sp or not sp["apply"] or endpoint != sp["apply"] or not isinstance(body, dict):
        return None
    hdr = {"Content-Type": "application/json"}
    url_v, url_a = _url(vmid, sp["validate"]), _url(vmid, sp["apply"])
    if not url_v:
        return (json.dumps({"error": "IP container introuvable"}), 404, hdr)
    try:
        # the dry run gets the same body minus the latency acceptance (a show adds its own flags, e.g.
        # `replace_scenes` — cf. app/plugin_shows.py — that the validation must see as well)
        rv = requests.post(url_v, json={k: v for k, v in body.items() if k != "accept_latency_jump"},
                           timeout=HTTP_TIMEOUT_S)
    except Exception as e:
        return (json.dumps({"error": str(e)}), 502, hdr)
    if rv.status_code != 200:
        return (rv.text, rv.status_code, hdr)
    try:
        canon = rv.json().get("doc")
    except ValueError:
        canon = None
    if not isinstance(canon, dict):
        return (json.dumps({"error": "validation sans document canonique"}), 502, hdr)
    dc = _dc(vmid) or {}
    old = (dc.get("params") or {}).get(sp["param"])
    _write(vmid, sp["param"], canon)
    try:
        ra = requests.post(url_a, json=body, timeout=HTTP_TIMEOUT_S)
        status, text = ra.status_code, ra.text
    except Exception as e:
        status, text = 502, json.dumps({"error": str(e)})
    if status != 200 and old is not None:
        _write(vmid, sp["param"], old, only_if=canon)
    if status == 200:
        try:                                     # arbre de macros dérivé du document : à refaire
            from .macros import invalidate_param_tree
            invalidate_param_tree(vmid)
        except Exception:
            pass
        from . import ops_journal                # ne lève jamais (cf. app/ops_journal.py)
        ops_journal.record_structure(vmid, m, endpoint, old, canon, actor=actor)
    return (text, status, hdr)
