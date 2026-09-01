# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Moteur de macros/scénarios (chantier 6, cf. docs/reference/PROJETS.md §7).

Exécution CÔTÉ ORCHESTRATEUR (fiabilité : fermer l'onglet n'interrompt rien ; point
unique pour shotbox, surfaces physiques, déclencheurs futurs). Les droits sont vérifiés
AU DÉCLENCHEMENT (routes) ; les étapes sont bornées au catalogue `actions` des plugins.

Formats stockés (`project_macros.graph`) — LES DEUX MOTEURS COHABITENT (décision
2026-07-10, 9e passe) :
  - `blocks/v1` : sérialisation du sous-ensemble STRUCTURÉ (séquence + blocs imbriqués
    dont les branches se rejoignent), produite par l'éditeur Macro à blocs. Exécuteur
    `_run_steps`, STRICTEMENT inchangé.
  - `nodes/v2` : graphe libre (nœuds + arêtes) pour l'éditeur Scénario nodal.
    Interpréteur à jetons `_run_graph` + conversion blocks↔graph (cf. sections en bas
    de fichier). Une macro blocks reste en blocks tant qu'elle est éditée en blocs
    (aucune migration silencieuse).

Étapes v1 :
  {type:"action", instance_uuid?, vmid?, action_id, params{}}   — POST :8082/<endpoint>
  {type:"sleep", ms}
  {type:"set_var", name, value}                                 — gabarits {{var}} OK
  {type:"if", cond, then:[...], else:[...]}
  {type:"parallel", branches:[[...], ...]}
  {type:"macro", macro_id}                                      — imbrication (profondeur ≤ 5)
  {type:"wait", cond, timeout_ms}                               — attendre qu'une condition
                                                                  devienne vraie (défaut 30 s)
  {type:"config", instance_uuid?/vmid?, params{clé: valeur}}    — écrit des champs du
        config_schema du plugin, MÊME chemin que la route plugin_config (8e passe)
  {type:"post", instance_uuid?/vmid?, endpoint, params{}}       — « action avancée » :
        POST libre sur un endpoint whitelisté (control.endpoints), params clé/valeur

Condition : {left:<opérande>, op:"=="|"!="|"<"|">"|"contains", right:<opérande>}
Opérande  : {kind:"const", value} | {kind:"var", name}
          | {kind:"state", instance_uuid?/vmid?, state_id}      — catalogue `state` du plugin
          | {kind:"state", instance_uuid?/vmid?, endpoint, path} — état DÉCOUVERT sur
            l'instance (borné aux control.read_endpoints, cf. discover_states)
"""

import json
import logging
import re
import threading
import time

import requests

from . import plugins
from .database import (db_get_container, db_get_containers, db_get_macro,
                       db_project_vars, db_set_project_var, db_add_alert,
                       db_all_enabled_triggers)
from .addressing import get_container_ip

log = logging.getLogger(__name__)

MAX_DEPTH = 5          # imbrication macro→macro (anti-boucle)
STEP_TIMEOUT_S = 5.0   # timeout HTTP par étape
JOURNAL_KEEP = 200     # entrées de journal conservées par run

# Un run à la fois par macro : {mid: _Run}
_runs = {}
_runs_lock = threading.Lock()


class _Run:
    def __init__(self, mid, user, allow_system_config=False):
        self.macro_id = mid
        self.user = user
        # Droit d'écrire les champs config_schema scope "system" (= containers.deploy),
        # capturé AU DÉCLENCHEMENT (le run tourne en thread, hors contexte requête).
        self.allow_system_config = bool(allow_system_config)
        self.running = True
        self.started = time.time()
        self.finished = None
        self.error = None
        self.journal = []      # [{ts, msg, ok} (+ node/event pour les runs nodaux)]
        self.active_nodes = set()   # nodes/v2 : nœuds en cours (surlignage live UI)
        self._cancel = threading.Event()

    def logline(self, msg, ok=True):
        self.journal.append({"ts": round(time.time() - self.started, 2),
                             "msg": msg, "ok": bool(ok)})
        del self.journal[:-JOURNAL_KEEP]

    def node_event(self, node_id, event, msg=None):
        """Événement par-nœud d'un run nodal (started/finished/error) : entrée de
        journal enrichie (node, event) + maintien de `active_nodes` pour l'UI."""
        if event == "started":
            self.active_nodes.add(node_id)
        else:
            self.active_nodes.discard(node_id)
        label = {"started": "début", "finished": "fin"}.get(event, event)
        self.journal.append({"ts": round(time.time() - self.started, 2),
                             "msg": msg or f"[{node_id}] {label}",
                             "ok": event != "error",
                             "node": node_id, "event": event})
        del self.journal[:-JOURNAL_KEEP]

    def snapshot(self):
        return {"macro_id": self.macro_id, "running": self.running,
                "user": self.user, "started": self.started,
                "finished": self.finished, "error": self.error,
                "active_nodes": sorted(self.active_nodes),
                "journal": list(self.journal)}


def _resolve_vmid(step):
    """instance_uuid prioritaire (stable au rechargement), repli vmid."""
    uuid = step.get("instance_uuid")
    if uuid:
        for c in db_get_containers():
            if c.get("instance_uuid") == uuid:
                return c["vmid"]
    return step.get("vmid")


def _tmpl(value, variables):
    """Gabarits {{var}} dans les valeurs texte des params/conditions."""
    if not isinstance(value, str):
        return value
    return re.sub(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}",
                  lambda m: str(variables.get(m.group(1), "")), value)


def exec_action(vmid, action_id, params, variables=None):
    """Exécute UNE action de plugin (aussi utilisée par la shotbox). Lève sur erreur."""
    c = db_get_container(vmid)
    if not c:
        raise RuntimeError(f"container #{vmid} introuvable")
    dc = c.get("deploy_config")
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    t = dc.get("type")
    act = next((a for a in ((plugins.get(t) or {}).get("actions") or [])
                if a.get("id") == action_id), None)
    if not act:
        raise RuntimeError(f"action « {action_id} » inconnue pour le type {t}")
    # Action CŒUR (pas un POST direct au conteneur) : `core: "recall"` = rappeler une
    # mémoire/preset du store ORCHESTRATEUR (control.recall du manifeste).
    if act.get("core") == "recall":
        return _exec_recall(vmid, t, act, params, variables)
    ip = get_container_ip(vmid)
    if not ip:
        raise RuntimeError(f"IP de #{vmid} introuvable")
    body = dict(act.get("body") or {})   # champs FIXES portés par l'action (ex. all:true)
    for pdef in (act.get("params") or []):
        k = pdef["key"]
        if params and k in params:
            v = _tmpl(params[k], variables or {})
            if pdef.get("type") == "number":
                try:
                    v = float(v)
                    v = int(v) if v == int(v) else v
                except (TypeError, ValueError):
                    pass
            elif pdef.get("type") == "bool":
                # Les valeurs arrivent en texte depuis l'UI : bool("false") serait True.
                v = str(v).strip().lower() in ("1", "true", "on", "oui", "yes")
            body[k] = v
        elif "default" in pdef and not pdef.get("optional"):
            body[k] = pdef["default"]
    # Port par action (défaut :8082 = contrôle plugin ; ex. multiview tally sur :8080).
    port = int(act.get("port") or 8082)
    r = requests.post(f"http://{ip}:{port}{act['endpoint']}", json=body,
                      timeout=STEP_TIMEOUT_S)
    if r.status_code >= 400:
        raise RuntimeError(f"{act['endpoint']} → HTTP {r.status_code}")
    return True


def _exec_recall(vmid, type_, act, params, variables):
    """Action « rappeler une mémoire » (`core: "recall"` au manifeste) : la liste des
    mémoires vient du store ORCHESTRATEUR (plugin_store ou layouts, selon control.recall)
    et l'application réutilise routes.recall_preset — même chemin que le provider Ember+.
    Le param dont `options_from == "recall"` porte l'id de la mémoire choisie."""
    from .routes import recall_presets, recall_preset   # lazy : évite l'import circulaire
    key = next((p.get("key") for p in (act.get("params") or [])
                if p.get("options_from") == "recall"), "memory")
    raw = _tmpl((params or {}).get(key), variables or {})
    try:
        mem_id = int(float(raw))
    except (TypeError, ValueError):
        raise RuntimeError(f"mémoire invalide : {raw!r}")
    _rc, presets = recall_presets(vmid, type_)
    idx = next((i for i, pr in enumerate(presets) if pr.get("id") == mem_id), None)
    if idx is None:
        raise RuntimeError(f"mémoire #{mem_id} introuvable (supprimée ?)")
    try:
        duration_ms = max(0, int(float(_tmpl((params or {}).get("duration_ms"),
                                             variables or {}) or 0)))
    except (TypeError, ValueError):
        duration_ms = 0
    ok, detail = recall_preset(vmid, type_, idx, duration_ms=duration_ms)
    if not ok:
        raise RuntimeError(f"rappel en échec : {detail}")
    return True


def _coerce_free(v):
    """Coercition prudente d'une valeur libre (POST avancé) : bool/nombre si la chaîne
    en a la tête, sinon la chaîne telle quelle. Les non-chaînes passent inchangées."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        f = float(s)
        return int(f) if f == int(f) and "." not in s and "e" not in s.lower() else f
    except ValueError:
        return v


def exec_config(vmid, params, variables=None, allow_system=False, confirm=False):
    """Écrit des champs déclaratifs (config_schema) d'un container plugin — RÉUTILISE le
    chemin d'écriture de la route plugin_config (validation scopes/clés, merge frais sous
    verrou, redeploy si le container tourne), en SYNCHRONE. Lève sur erreur."""
    from .routes.plugin_registry import apply_plugin_config, PluginConfigError  # lazy : évite l'import circulaire
    vals = {k: _tmpl(v, variables or {}) for k, v in (params or {}).items()}
    if not vals:
        raise RuntimeError("réglage sans champ")
    try:
        apply_plugin_config(vmid, vals, allow_system=allow_system, confirm=confirm)
    except PluginConfigError as e:
        raise RuntimeError(str(e))
    return True


def exec_post(vmid, endpoint, params, variables=None):
    """« Action avancée » générique : POST sur un endpoint whitelisté du plugin
    (control.endpoints — même bornage que le proxy), params libres clé/valeur.
    La découverte n'ouvre aucune nouvelle surface : endpoint non listé → refus."""
    c = db_get_container(vmid)
    if not c:
        raise RuntimeError(f"container #{vmid} introuvable")
    dc = c.get("deploy_config")
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    t = dc.get("type")
    m = plugins.get(t) or {}
    endpoint = str(endpoint or "")
    ep = next((e for e in plugins.control_post_endpoints(m) if e["path"] == endpoint), None)
    if not ep:
        raise RuntimeError(f"{endpoint or '(vide)'} n'est pas whitelisté pour le type {t}")
    ip = get_container_ip(vmid)
    if not ip:
        raise RuntimeError(f"IP de #{vmid} introuvable")
    body = {k: _coerce_free(_tmpl(v, variables or {}))
            for k, v in (params or {}).items() if str(k).strip()}
    r = requests.post(f"http://{ip}:{ep['port']}{endpoint}",
                      json=body, timeout=STEP_TIMEOUT_S)
    if r.status_code >= 400:
        raise RuntimeError(f"{endpoint} → HTTP {r.status_code}")
    return True


def exec_service_action(service_id, action_id, params, pid=None, user=None,
                        variables=None):
    """Exécute une ACTION DE SERVICE (tsl.set_label, skaarhoj.assign_key…) — in-process,
    validée contre le manifeste du service (availability project|system selon contexte).
    Les gabarits {{var}} s'appliquent aux valeurs texte."""
    from . import core_plugins
    vals = {k: _tmpl(v, variables or {}) for k, v in (params or {}).items()}
    return core_plugins.run_service_action(service_id, action_id, vals,
                                           ctx={"project_id": pid, "user": user})


def _walk_path(val, path):
    """Descend un chemin pointé dans du JSON (dicts + listes indexées : `jobs.0.state`)."""
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list) and part.isdigit() and int(part) < len(val):
            val = val[int(part)]
        else:
            return None
    return val


def fetch_state(vmid, state_id=None, endpoint=None, path=None):
    """Lit UNE valeur d'état (feedback/conditions) :
    - `state_id` → catalogue `state` curaté du plugin (comme avant) ;
    - sinon `endpoint`+`path` → état DÉCOUVERT, borné aux `control.read_endpoints`
      (même chemin réseau, aucune nouvelle surface)."""
    c = db_get_container(vmid)
    if not c:
        return None
    dc = c.get("deploy_config")
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    t = dc.get("type")
    m = plugins.get(t) or {}
    if state_id:
        sdef = next((s for s in (m.get("state") or []) if s.get("id") == state_id), None)
        if not sdef:
            return None
    elif endpoint:
        ctrl = m.get("control") or {}
        if endpoint not in set(ctrl.get("read_endpoints") or []):
            return None
        sdef = {"endpoint": endpoint, "path": path, "port": ctrl.get("port")}
    else:
        return None
    ip = get_container_ip(vmid)
    if not ip:
        return None
    try:
        r = requests.get(f"http://{ip}:{int(sdef.get('port') or 8082)}"
                         f"{sdef.get('endpoint') or '/state'}", timeout=1.5)
        return _walk_path(r.json(), sdef.get("path"))
    except Exception:
        return None


# ─── Découverte d'états sur l'instance live (8e passe ch.6) ─────────────
#
# `discover_states(vmid)` interroge les `control.read_endpoints` du plugin (même chemin
# réseau que fetch_state), aplatit récursivement le JSON en chemins pointés
# (`params.saturation`, listes indexées avec prudence), infère le type, et renvoie des
# états utilisables dans les conditions/feedback/triggers via l'opérande
# {kind:"state", endpoint, path}. Cache court (15 s), échec silencieux par endpoint.

DISCOVER_CACHE_S = 15.0
DISCOVER_MAX_DEPTH = 4      # profondeur d'aplatissement
DISCOVER_MAX_ITEMS = 150    # nb max d'états renvoyés par instance
DISCOVER_LIST_MAX = 8       # nb max d'éléments de liste indexés

_disc_cache = {}            # vmid → (ts, result)
_disc_lock = threading.Lock()


def _flatten_state_json(val, prefix, out, depth=0):
    if len(out) >= DISCOVER_MAX_ITEMS:
        return
    if isinstance(val, dict):
        if depth >= DISCOVER_MAX_DEPTH:
            return
        for k, v in val.items():
            _flatten_state_json(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(val, list):
        if depth >= DISCOVER_MAX_DEPTH:
            return
        for i, v in enumerate(val[:DISCOVER_LIST_MAX]):
            _flatten_state_json(v, f"{prefix}.{i}" if prefix else str(i), out, depth + 1)
    elif prefix:   # feuille (le document racine entier n'est pas un état)
        t = ("bool" if isinstance(val, bool)
             else "number" if isinstance(val, (int, float)) else "text")
        out.append({"path": prefix, "type": t, "value": val})


def discover_states(vmid):
    """États découverts sur l'instance live. Renvoie
    {states:[{id,label,endpoint,path,type,value,source:"discovered"}], partial:bool}.
    `partial` = au moins un endpoint injoignable/illisible (instance éteinte…)."""
    now = time.monotonic()
    with _disc_lock:
        hit = _disc_cache.get(vmid)
        if hit and now - hit[0] < DISCOVER_CACHE_S:
            return hit[1]
    result = {"states": [], "partial": False}
    c = db_get_container(vmid)
    dc = c.get("deploy_config") if c else None
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    m = plugins.get(dc.get("type")) or {}
    ctrl = m.get("control") or {}
    read_eps = list(ctrl.get("read_endpoints") or [])
    ip = get_container_ip(vmid) if c else None
    if not ip:
        result["partial"] = bool(read_eps)
        read_eps = []
    port = int(ctrl.get("port") or 8082)
    for ep in read_eps:
        try:
            r = requests.get(f"http://{ip}:{port}{ep}", timeout=1.5)
            doc = r.json()
        except Exception:
            result["partial"] = True   # instance éteinte / endpoint non-JSON (preview…)
            continue
        flat = []
        _flatten_state_json(doc, "", flat)
        for f in flat:
            result["states"].append({
                "id": f"disc:{ep}#{f['path']}",
                "label": (f["path"] if ep == "/state" else ep.lstrip("/") + " · " + f["path"]),
                "endpoint": ep, "path": f["path"], "type": f["type"],
                "value": f["value"], "source": "discovered"})
    with _disc_lock:
        _disc_cache[vmid] = (now, result)
    return result


# ─── Arbre de paramètres (chantier convivialité macros, 2026-07) ──────────────
# Le plugin déclare la STRUCTURE (`param_tree` du manifeste : boxes, groupes, libellés
# humains) ; les BORNES [min,max,défaut] viennent EN DIRECT de `/state.caps` (règle du
# projet : aucune constante de format en dur, l'UI lit caps). On fusionne les deux + les
# libellés d'entrées câblées (`wiring.consumes`) → un arbre résolu élément→groupe→paramètre.
# Chaque feuille porte de quoi POSER une valeur (endpoint + idx + wrap + key) ET la LIRE
# (path dans /state) — même paramètre, deux usages (action « régler » et condition
# « attendre »). Cache court comme discover_states ; instance éteinte → partial:true.

PARAMTREE_CACHE_S = 15.0
_pt_cache = {}
_pt_lock = threading.Lock()


def _caps_bounds(caps, family_key, field):
    """Bornes [min,max,défaut] d'un champ depuis /state.caps. Une famille peut être un
    dict {champ:[min,max,def]} (box_fields…) ou une liste unique [min,max,def] partagée
    par tous les champs du groupe (crop). Renvoie None si absent/mal formé."""
    fam = (caps or {}).get(family_key)
    b = fam.get(field) if isinstance(fam, dict) else (fam if isinstance(fam, list) else None)
    if not isinstance(b, list) or len(b) < 2:
        return None
    mn, mx = b[0], b[1]
    dflt = b[2] if len(b) > 2 else mn
    is_int = all(isinstance(v, int) and not isinstance(v, bool) for v in (mn, mx))
    return {"min": mn, "max": mx, "default": (0 if dflt is None else dflt),
            "step": 1 if is_int else 0.01}


def _pt_group(g, caps, path_prefix, endpoint_idx):
    """Résout un groupe (box ou global) en liste de feuilles-paramètres.

    Un champ du manifeste est soit un libellé (string, type numérique par défaut), soit un
    objet {label, type, ...}. Le `type` est SÉMANTIQUE (position/angle/percent/pixels/
    ratio/scale/duration/bool/enum) → l'UI en déduit l'unité et le widget.

    Les BORNES (numériques) et OPTIONS (enum) viennent, par ordre de préférence :
    1. EN DIRECT de `/state.caps` (contrat riche, cas split — canonique, jamais périmé) ;
    2. À défaut, DÉCLARÉES dans le manifeste (`min`/`max`/`step`/`options`) — pour les
       plugins sans contrat caps. Jamais inventées : un champ sans caps NI déclaration est
       sauté (dégradation sûre, pas d'échec muet)."""
    wrap = g.get("wrap")
    out = []
    for key, fv in (g.get("fields") or {}).items():
        spec = fv if isinstance(fv, dict) else {"label": fv}
        label = spec.get("label") or key
        ftype = spec.get("type") or "number"
        # Chemin de LECTURE dans /state : par défaut path_prefix[.wrap].clé, mais surchargeable
        # par `path` quand la clé POST diffère du nom d'état (ex mixer : POST `enabled` →
        # état `overlay_enabled`). Sinon la condition lirait un chemin inexistant.
        path = spec.get("path") or ".".join([p for p in (path_prefix, wrap, key) if p])
        leaf = {"key": key, "label": label, "type": ftype, "endpoint": g.get("endpoint"),
                "wrap": wrap, "idx": endpoint_idx, "path": path}
        if ftype == "bool":
            leaf["default"] = bool(spec.get("default", False))
        elif ftype == "text":
            leaf["default"] = spec.get("default", "")   # champ libre : pas de bornes
        elif ftype == "enum":
            opts = (caps.get(spec.get("enum_caps") or "") or {}).get(key) or spec.get("options")
            if not opts:
                continue   # options ni dans caps ni déclarées → on ne devine pas
            leaf["options"] = list(opts)
            leaf["default"] = spec.get("default", opts[0])
        else:
            bnd = _caps_bounds(caps, g.get("caps"), key) or _declared_bounds(spec)
            if bnd is None:
                continue   # ni caps ni bornes déclarées → sauté (pas d'échec muet)
            leaf.update(bnd)
        out.append(leaf)
    return {"label": g.get("label"), "params": out}


def _declared_bounds(spec):
    """Bornes déclarées EN DUR dans le manifeste (plugins sans contrat caps). None si le
    champ ne porte pas min/max."""
    mn, mx = spec.get("min"), spec.get("max")
    if mn is None or mx is None:
        return None
    is_int = all(isinstance(v, int) and not isinstance(v, bool) for v in (mn, mx))
    return {"min": mn, "max": mx, "default": spec.get("default", mn),
            "step": spec.get("step", 1 if is_int else 0.01)}


def param_tree(vmid):
    """Arbre de paramètres résolu de l'instance (structure manifeste × caps live).
    {type, elements:[{id,label,kind,idx,groups:[{label,params:[{key,label,endpoint,
    wrap,idx,path,min,max,step,default}]}]}], partial}."""
    now = time.monotonic()
    with _pt_lock:
        hit = _pt_cache.get(vmid)
        if hit and now - hit[0] < PARAMTREE_CACHE_S:
            return hit[1]
    c = db_get_container(vmid)
    dc = c.get("deploy_config") if c else None
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    t = dc.get("type")
    m = plugins.get(t) or {}
    spec = m.get("param_tree") or {}
    result = {"type": t, "elements": [], "partial": False}
    if not spec:
        return result
    # /state live : caps (bornes) + n_boxes.
    ctrl = m.get("control") or {}
    ip = get_container_ip(vmid) if c else None
    doc = None
    if ip:
        try:
            r = requests.get(f"http://{ip}:{int(ctrl.get('port') or 8082)}/state", timeout=1.5)
            doc = r.json()
        except Exception:
            doc = None
    # /state absent (instance éteinte, ou plugin sans /state) → on construit quand même
    # l'arbre depuis le manifeste (bornes déclarées), sans caps ni valeurs live. `partial`
    # signale que les LECTURES d'état (conditions) ne sont pas garanties ; les ÉCRITURES
    # (actions) restent valides.
    if not isinstance(doc, dict):
        result["partial"] = True
        doc = {}
    caps = doc.get("caps") or {}
    # Libellés des entrées câblées (« Box 1 »…« Fond ») depuis wiring.consumes.
    p = dc.get("params") or {}
    hn = p.get("hostname") or (c.get("hostname") if c else "") or ""
    slot_label = {}
    try:
        for cons in plugins.derive_wiring(t, hn, p)["consumes"]:
            if cons.get("slot") is not None:
                slot_label[cons["slot"]] = cons.get("label")
    except Exception:
        pass
    elements = []
    boxes = spec.get("boxes") or {}
    if boxes:
        # Compte d'éléments : fixe (`count`) OU lu dans /state (`count_from`).
        n = boxes.get("count")
        if n is None:
            n = doc.get(boxes.get("count_from") or "n_boxes")
        n = int(n or 0)
        prefix = boxes.get("path_prefix") or "boxes"
        label_prefix = boxes.get("label") or "Box"   # « Box 1 » (split) / « PiP 1 » (multiview)…
        for i in range(n):
            groups = [_pt_group(g, caps, f"{prefix}.{i}", i) for g in (spec.get("box_groups") or [])]
            groups = [g for g in groups if g["params"]]
            elements.append({"id": f"box:{i}", "kind": "box", "idx": i,
                             "label": slot_label.get(i) or f"{label_prefix} {i + 1}", "groups": groups})
    gg = [_pt_group(g, caps, g.get("path_prefix") or "", None) for g in (spec.get("global_groups") or [])]
    gg = [g for g in gg if g["params"]]
    if gg:
        elements.append({"id": "global", "kind": "global", "idx": None,
                         "label": "Général", "groups": gg})
    result["elements"] = [e for e in elements if e["groups"]]
    with _pt_lock:
        _pt_cache[vmid] = (now, result)
    return result


def _operand(o, pid, variables):
    o = o or {}
    kind = o.get("kind") or "const"
    if kind == "const":
        return _tmpl(o.get("value"), variables)
    if kind == "var":
        return variables.get(o.get("name"))
    if kind == "state":
        vmid = _resolve_vmid(o)
        # state_id (catalogue curaté) OU endpoint+path (état découvert) — même format.
        return fetch_state(vmid, o.get("state_id"),
                           endpoint=o.get("endpoint"), path=o.get("path")) if vmid else None
    return None


def eval_cond(cond, pid, variables):
    left = _operand(cond.get("left"), pid, variables)
    right = _operand(cond.get("right"), pid, variables)
    op = cond.get("op") or "=="
    if op == "contains":
        return str(right or "") in str(left or "")
    if op in ("<", ">"):
        try:
            l, r = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return l < r if op == "<" else l > r
    eq = str(left).strip().lower() == str(right).strip().lower()
    return eq if op == "==" else not eq


def _exec_leaf_step(step, pid, run):
    """Exécute UNE étape FEUILLE (action/config/post/sleep/wait/set_var) — code partagé
    TEL QUEL entre l'exécuteur blocks/v1 (`_run_steps`) et l'interpréteur nodal nodes/v2
    (un nœud feuille = la même étape). Renvoie False si le type n'est pas une feuille
    (l'appelant gère les types composés ou refuse). Lève sur erreur d'exécution."""
    t = step.get("type")
    if t not in ("action", "config", "post", "sleep", "wait", "set_var"):
        return False
    variables = db_project_vars(pid) if pid else {}
    if t == "action":
        if step.get("service"):
            exec_service_action(step["service"], step.get("action_id"),
                                step.get("params") or {}, pid=pid,
                                user=run.user, variables=variables)
            run.logline(f"service {step['service']}.{step.get('action_id')}")
        else:
            vmid = _resolve_vmid(step)
            if not vmid:
                raise RuntimeError("cible d'action introuvable (container disparu ?)")
            exec_action(vmid, step.get("action_id"), step.get("params") or {}, variables)
            run.logline(f"action {step.get('action_id')} → #{vmid}")
    elif t == "config":
        vmid = _resolve_vmid(step)
        if not vmid:
            raise RuntimeError("cible de réglage introuvable (container disparu ?)")
        exec_config(vmid, step.get("params") or {}, variables,
                    allow_system=run.allow_system_config,
                    confirm=bool(step.get("confirm")))
        run.logline(f"réglage {', '.join((step.get('params') or {}).keys())} → #{vmid}")
    elif t == "post":
        vmid = _resolve_vmid(step)
        if not vmid:
            raise RuntimeError("cible d'appel introuvable (container disparu ?)")
        exec_post(vmid, step.get("endpoint"), step.get("params") or {}, variables)
        run.logline(f"POST {step.get('endpoint')} → #{vmid}")
    elif t == "sleep":
        ms = max(0, int(step.get("ms") or 0))
        run.logline(f"pause {ms} ms")
        if run._cancel.wait(timeout=ms / 1000.0):
            raise RuntimeError("annulé")
    elif t == "wait":
        # Attendre qu'une condition devienne vraie (borné par timeout_ms).
        # Pas de busy-loop : cadence 0,3 s via l'Event d'annulation.
        cond = step.get("cond") or {}
        timeout_ms = max(0, int(step.get("timeout_ms") or 30000))
        run.logline(f"attente… (max {timeout_ms} ms)")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if eval_cond(cond, pid, db_project_vars(pid) if pid else {}):
                run.logline("condition atteinte")
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("délai d'attente dépassé")
            if run._cancel.wait(timeout=0.3):
                raise RuntimeError("annulé")
    elif t == "set_var":
        if pid:
            db_set_project_var(pid, step.get("name"), _tmpl(step.get("value"), variables))
        run.logline(f"var {step.get('name')} = {step.get('value')}")
    return True


def _run_steps(steps, pid, run, depth):
    for step in (steps or []):
        if run._cancel.is_set():
            raise RuntimeError("annulé")
        t = step.get("type")
        if t == "if":
            variables = db_project_vars(pid) if pid else {}
            branch = eval_cond(step.get("cond") or {}, pid, variables)
            run.logline(f"si → {'alors' if branch else 'sinon'}")
            _run_steps(step.get("then") if branch else step.get("else"), pid, run, depth)
        elif t == "parallel":
            branches = step.get("branches") or []
            run.logline(f"parallèle ({len(branches)} branches)")
            errs = []
            def _br(b):
                try:
                    _run_steps(b, pid, run, depth)
                except Exception as e:
                    errs.append(str(e))
            threads = [threading.Thread(target=_br, args=(b,), daemon=True)
                       for b in branches]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=120)
            if errs:
                raise RuntimeError(" ; ".join(errs))
        elif t == "macro":
            if depth >= MAX_DEPTH:
                raise RuntimeError(f"imbrication de macros > {MAX_DEPTH}")
            sub = db_get_macro(step.get("macro_id"))
            if not sub:
                raise RuntimeError(f"macro #{step.get('macro_id')} introuvable")
            run.logline(f"macro « {sub['name']} »")
            _run_subgraph(sub.get("graph") or {}, pid, run, depth + 1)
        elif not _exec_leaf_step(step, pid, run):
            raise RuntimeError(f"étape inconnue : {t}")


def _run_subgraph(graph, pid, run, depth):
    """Exécute le graph d'une SOUS-macro (étape/nœud `macro`) dans le bon moteur :
    blocks/v1 → _run_steps, nodes/v2 → _run_graph. Synchrone, même run/journal."""
    if (graph or {}).get("format") == "nodes/v2":
        _run_graph(graph, pid, run, depth)
    else:
        _run_steps((graph or {}).get("steps"), pid, run, depth)


# ─── Moteur nodal `nodes/v2` (9e passe ch.6) ────────────────────────────────
#
# Format : {format:"nodes/v2", nodes:[{id,type,params,x,y}], edges:[{from,port,to}]}.
# Les DEUX moteurs cohabitent (décision 2026-07-10) : blocks/v1 reste STRICTEMENT
# inchangé ; nodes/v2 est un interpréteur de graphe À JETONS :
#   - un jeton démarre à chaque nœud `entry` activé (mode manual, ou entry_id précis) ;
#   - plusieurs arêtes sortantes d'un même port = fan-out parallèle (threads, comme le
#     bloc parallel) ; le jeton meurt en bout de chemin ;
#   - `cond` route port 0 (vrai) / port 1 (faux) ; `choice` évalue params.branches dans
#     l'ordre (port i) avec défaut (port N) ;
#   - `join` mode "all" : compteur d'arrivées vs nb d'arêtes entrantes ATTEIGNABLES
#     depuis les entrées activées (choix pragmatique v1, documenté : une branche tuée en
#     amont par un cond n'arrivera jamais → la jointure ne se déclenche pas ; re-armée
#     après déclenchement pour les boucles). mode "any" : la première arrivée passe, les
#     suivantes meurent (pour tout le run) ;
#   - boucles autorisées mais bornées : NODE_VISITS_MAX passages par nœud ;
#   - mêmes annulation/journal/timeouts que blocks ; événements par nœud
#     (started/finished/error) dans le journal + `active_nodes` dans le snapshot ;
#   - nœuds feuilles = MÊMES exécutions unitaires que les étapes blocks
#     (_exec_leaf_step) ; `macro` = même borne de profondeur (MAX_DEPTH).
# Les entrées `mode:"trigger"` sont stockées/validées mais PAS encore évaluées par le
# poller (différé, cf. docs/reference/PROJETS.md) : un trigger classique pointant sur une macro nodale
# démarre ses entrées manual.

GRAPH_FORMAT = "nodes/v2"
GRAPH_NODE_TYPES = ("entry", "action", "config", "post", "sleep", "set_var",
                    "wait", "macro", "cond", "choice", "join")
NODE_VISITS_MAX = 1000   # plafond de passages par nœud (boucles bornées)


def _node_out_ports(node):
    """Nombre de ports de sortie d'un nœud selon son type."""
    t = node.get("type")
    if t == "cond":
        return 2
    if t == "choice":
        return len(((node.get("params") or {}).get("branches")) or []) + 1  # + défaut
    return 1


def validate_graph(graph):
    """Valide un graphe nodes/v2. Renvoie la liste des erreurs (vide = valide)."""
    if not isinstance(graph, dict) or graph.get("format") != GRAPH_FORMAT:
        return [f"format attendu : {GRAPH_FORMAT}"]
    nodes, edges = graph.get("nodes"), graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return ["nodes/edges doivent être des listes"]
    errs, by_id = [], {}
    for n in nodes:
        nid = (n or {}).get("id") if isinstance(n, dict) else None
        if not nid or not isinstance(nid, str):
            errs.append("nœud sans id")
            continue
        if nid in by_id:
            errs.append(f"id de nœud dupliqué : {nid}")
        by_id[nid] = n
        if n.get("type") not in GRAPH_NODE_TYPES:
            errs.append(f"type de nœud inconnu : {n.get('type')} ({nid})")
    for e in edges:
        if not isinstance(e, dict):
            errs.append("arête invalide")
            continue
        f, to = e.get("from"), e.get("to")
        if f not in by_id:
            errs.append(f"arête depuis un nœud inconnu : {f}")
        if to not in by_id:
            errs.append(f"arête vers un nœud inconnu : {to}")
        try:
            port = int(e.get("port") or 0)
        except (TypeError, ValueError):
            port = -1
        if f in by_id and not (0 <= port < _node_out_ports(by_id[f])):
            errs.append(f"port {e.get('port')} invalide sur {f}")
    if not any(n.get("type") == "entry" for n in nodes if isinstance(n, dict)):
        errs.append("aucune entrée (nœud entry)")
    return errs


class _GraphCtx:
    """Contexte d'un run nodal : topologie, jetons (threads), jointures, garde-fous."""

    def __init__(self, graph, pid, run, depth):
        self.nodes = {n["id"]: n for n in graph.get("nodes") or []}
        self.out = {}    # nid → port → [to, …] (ordre des arêtes = ordre des branches)
        self.edges = list(graph.get("edges") or [])
        for e in self.edges:
            self.out.setdefault(e["from"], {}).setdefault(
                int(e.get("port") or 0), []).append(e["to"])
        self.pid, self.run, self.depth = pid, run, depth
        self.lock = threading.Lock()
        self.visits = {}         # nid → nb de passages (boucles bornées)
        self.errors = []         # erreurs de jetons (le run continue, cf. bloc parallel)
        self.threads = []        # tous les jetons (y compris spawnés en cours de route)
        self.join_state = {}     # nid → {"arrived": int, "fired": bool}
        self.expected_in = {}    # join nid → nb d'arêtes entrantes atteignables

    def reachable(self, starts):
        """Nœuds atteignables depuis `starts` en suivant TOUTES les arêtes (tous ports —
        pragmatique : un cond ne prendra qu'une branche au runtime, on ne le sait pas)."""
        seen, stack = set(starts), list(starts)
        while stack:
            for port_targets in (self.out.get(stack.pop()) or {}).values():
                for to in port_targets:
                    if to not in seen:
                        seen.add(to)
                        stack.append(to)
        return seen

    def spawn(self, nid):
        th = threading.Thread(target=self._token, args=(nid,), daemon=True)
        with self.lock:
            self.threads.append(th)
        th.start()

    def wait_all(self):
        i = 0
        while True:
            with self.lock:
                if i >= len(self.threads):
                    break
                th = self.threads[i]
            th.join()
            i += 1

    def _join_pass(self, nid, node):
        """Arrivée d'un jeton sur une jointure. True = le jeton continue."""
        with self.lock:
            st = self.join_state.setdefault(nid, {"arrived": 0, "fired": False})
            mode = (node.get("params") or {}).get("mode") or "all"
            if mode == "any":
                if st["fired"]:
                    return False   # premier arrivé déjà passé : le jeton meurt
                st["fired"] = True
                return True
            st["arrived"] += 1
            if st["arrived"] < max(1, self.expected_in.get(nid, 1)):
                return False       # on attend les autres branches : le jeton meurt
            st["arrived"] = 0      # ré-armée (boucles à travers la jointure)
            return True

    def _token(self, nid):
        """Boucle d'un jeton : exécute le nœud courant puis suit le port de sortie ;
        fan-out = jetons supplémentaires. Une erreur tue le jeton (les autres continuent,
        comme les branches du bloc parallel) et sera remontée en fin de run."""
        run = self.run
        try:
            while nid is not None:
                if run._cancel.is_set():
                    raise RuntimeError("annulé")
                node = self.nodes[nid]
                with self.lock:
                    self.visits[nid] = self.visits.get(nid, 0) + 1
                    if self.visits[nid] > NODE_VISITS_MAX:
                        raise RuntimeError(
                            f"boucle : nœud {nid} exécuté plus de {NODE_VISITS_MAX} fois")
                if node.get("type") == "join" and not self._join_pass(nid, node):
                    return   # le jeton meurt à la jointure (journal silencieux)
                run.node_event(nid, "started")
                try:
                    port = self._exec_node(nid, node)
                except Exception as e:
                    run.node_event(nid, "error", msg=f"[{nid}] échec : {e}")
                    with self.lock:
                        self.errors.append(f"nœud {nid} : {e}")
                    return
                run.node_event(nid, "finished")
                nexts = (self.out.get(nid) or {}).get(port) or []
                if not nexts:
                    return
                nid = nexts[0]
                for extra in nexts[1:]:
                    self.spawn(extra)
        except Exception as e:
            with self.lock:
                self.errors.append(str(e))

    def _exec_node(self, nid, node):
        """Exécute UN nœud, renvoie le port de sortie à suivre."""
        t = node.get("type")
        params = node.get("params") or {}
        if t in ("entry", "join"):
            return 0
        if t == "cond":
            variables = db_project_vars(self.pid) if self.pid else {}
            return 0 if eval_cond(params.get("cond") or {}, self.pid, variables) else 1
        if t == "choice":
            branches = params.get("branches") or []
            variables = db_project_vars(self.pid) if self.pid else {}
            for i, br in enumerate(branches):
                cond = br.get("cond") if isinstance(br, dict) and "cond" in br else br
                if eval_cond(cond or {}, self.pid, variables):
                    return i
            return len(branches)   # port défaut
        if t == "macro":
            if self.depth >= MAX_DEPTH:
                raise RuntimeError(f"imbrication de macros > {MAX_DEPTH}")
            sub = db_get_macro(params.get("macro_id"))
            if not sub:
                raise RuntimeError(f"macro #{params.get('macro_id')} introuvable")
            self.run.logline(f"macro « {sub['name']} »")
            _run_subgraph(sub.get("graph") or {}, self.pid, self.run, self.depth + 1)
            return 0
        # Nœud feuille = même exécution unitaire que l'étape blocks correspondante.
        step = dict(params)
        step["type"] = t
        if not _exec_leaf_step(step, self.pid, self.run):
            raise RuntimeError(f"nœud inconnu : {t}")
        return 0


def _run_graph(graph, pid, run, depth, entry_id=None):
    """Exécute un graphe nodes/v2 (synchrone : rend la main quand tous les jetons sont
    morts). `entry_id` = démarrer sur UNE entrée précise ; sinon toutes les entrées
    `manual`. Lève à la fin si au moins un jeton a échoué (comme le bloc parallel)."""
    errs = validate_graph(graph)
    if errs:
        raise RuntimeError("graphe invalide : " + " ; ".join(errs))
    ctx = _GraphCtx(graph, pid, run, depth)
    if entry_id:
        node = ctx.nodes.get(entry_id)
        if not node or node.get("type") != "entry":
            raise RuntimeError(f"entrée {entry_id} introuvable")
        starts = [entry_id]
    else:
        starts = [n["id"] for n in graph["nodes"] if n.get("type") == "entry"
                  and ((n.get("params") or {}).get("mode") or "manual") == "manual"]
        if not starts:
            raise RuntimeError("aucune entrée manuelle (entries trigger seulement)")
    # `join all` : arrivées attendues = arêtes entrantes depuis les nœuds atteignables
    # des entrées ACTIVÉES (statique, pragmatique v1 — cf. bandeau de section).
    reach = ctx.reachable(starts)
    for e in ctx.edges:
        to = e.get("to")
        if (ctx.nodes.get(to) or {}).get("type") == "join" and e.get("from") in reach:
            ctx.expected_in[to] = ctx.expected_in.get(to, 0) + 1
    for nid in starts:
        ctx.spawn(nid)
    ctx.wait_all()
    if ctx.errors:
        raise RuntimeError(" ; ".join(ctx.errors))


# ─── Conversion blocks/v1 ↔ nodes/v2 (compilation + détection structurée) ───
#
# `blocks_to_graph` : compilation SANS PERTE blocs → graphe (positions x,y par layout en
# couches gauche→droite). Un bloc `if` = nœud cond dont les deux branches convergent sur
# l'étape suivante ; un bloc `parallel` = fan-out + nœud join "all". Quand un parallel
# suit un if (plusieurs queues), un join "any" est inséré comme point de convergence
# explicite (un seul jeton vivant → sémantique inchangée, et la structure reste
# détectable). `graph_to_blocks` : reconstruction (lève UnstructuredGraph si le graphe
# n'est pas série-parallèle bien imbriqué : une seule entry manual, régions cond/parallel
# refermées, pas de saut entre branches, pas de cycle, pas de choice/join any libres).
# Round-trip garanti : graph_to_blocks(blocks_to_graph(b)) == b (modulo normalisation
# then/else/branches absents → listes vides).


class UnstructuredGraph(Exception):
    """Graphe non représentable en blocs (badge « avancé » côté UI/API)."""


def blocks_to_graph(steps):
    """Compile une liste d'étapes blocks/v1 en graphe nodes/v2 équivalent."""
    nodes, edges, seq = [], [], [0]

    def _new(t, params):
        seq[0] += 1
        n = {"id": f"n{seq[0]}", "type": t, "params": params, "x": 0, "y": 0}
        nodes.append(n)
        return n["id"]

    def _attach(tails, to):
        for f, p in tails:
            edges.append({"from": f, "port": p, "to": to})

    def _build(steps, tails):
        """Compile une séquence à partir des points d'attache `tails` [(nid, port)] ;
        renvoie les queues de la séquence."""
        for step in steps or []:
            t = step.get("type")
            if t == "if":
                c = _new("cond", {"cond": step.get("cond") or {}})
                _attach(tails, c)
                tails = (_build(step.get("then") or [], [(c, 0)])
                         + _build(step.get("else") or [], [(c, 1)]))
            elif t == "parallel":
                if len(tails) > 1:   # point de convergence explicite avant le fan-out
                    m = _new("join", {"mode": "any"})
                    _attach(tails, m)
                    tails = [(m, 0)]
                j = _new("join", {"mode": "all"})
                for b in (step.get("branches") or []):
                    _attach(_build(b or [], list(tails)), j)
                tails = [(j, 0)]
            else:
                n = _new(t, {k: v for k, v in step.items() if k != "type"})
                _attach(tails, n)
                tails = [(n, 0)]
        return tails

    e = _new("entry", {"mode": "manual"})
    _build(steps or [], [(e, 0)])
    _layout(nodes, edges)
    return {"format": GRAPH_FORMAT, "nodes": nodes, "edges": edges}


def _layout(nodes, edges):
    """Positions x,y par couches (plus long chemin depuis les racines, gauche→droite)."""
    layer = {n["id"]: 0 for n in nodes}
    # Itération bornée (le graphe compilé est un DAG ; borne = nb de nœuds passes).
    for _ in range(len(nodes)):
        moved = False
        for e in edges:
            if layer[e["to"]] < layer[e["from"]] + 1:
                layer[e["to"]] = layer[e["from"]] + 1
                moved = True
        if not moved:
            break
    lanes = {}
    for n in nodes:
        l = layer[n["id"]]
        lane = lanes.get(l, 0)
        lanes[l] = lane + 1
        n["x"], n["y"] = 60 + 230 * l, 60 + 110 * lane


def graph_to_blocks(graph):
    """Reconstruit la forme blocs d'un graphe nodes/v2 STRUCTURÉ (série-parallèle bien
    imbriqué). Lève UnstructuredGraph sinon — jamais de conversion avec perte."""
    errs = validate_graph(graph)
    if errs:
        raise UnstructuredGraph("graphe invalide : " + " ; ".join(errs))
    nodes = {n["id"]: n for n in graph["nodes"]}
    outs, succ = {}, {}
    for i, e in enumerate(graph.get("edges") or []):
        outs.setdefault((e["from"], int(e.get("port") or 0)), []).append((e["to"], i))
        succ.setdefault(e["from"], []).append(e["to"])
    entries = [n for n in graph["nodes"] if n.get("type") == "entry"]
    if len(entries) != 1:
        raise UnstructuredGraph("plusieurs entrées")
    if ((entries[0].get("params") or {}).get("mode") or "manual") != "manual":
        raise UnstructuredGraph("entrée déclencheur")
    used, consumed, _reach_memo = set(), set(), {}

    def _fail(msg):
        raise UnstructuredGraph(msg)

    def _reach(nid):
        """Nœuds atteignables depuis nid, nid INCLUS (memoïsé)."""
        if nid in _reach_memo:
            return _reach_memo[nid]
        seen, stack = {nid}, [nid]
        while stack:
            for to in succ.get(stack.pop(), []):
                if to not in seen:
                    seen.add(to)
                    stack.append(to)
        _reach_memo[nid] = seen
        return seen

    def _merge_of(head_groups):
        """Point de convergence de plusieurs branches (listes de têtes) : l'unique nœud
        commun qui atteint tous les autres nœuds communs. None si aucune convergence."""
        sets = []
        for heads in head_groups:
            r = set()
            for h in heads:
                r |= _reach(h)
            sets.append(r)
        common = set.intersection(*sets) if sets else set()
        if not common:
            return None
        cands = [c for c in common if common <= _reach(c)]
        if len(cands) != 1:
            _fail("convergence ambiguë (cycle ?)")
        return cands[0]

    def _consume(nid):
        if nid in consumed:
            _fail(f"nœud {nid} atteint deux fois (saut entre branches ou cycle)")
        consumed.add(nid)

    def _parse_outs(fid, port, stop):
        """Étapes à partir des arêtes sortantes de (fid, port), jusqu'à `stop` exclu
        (None = fin de graphe). Fan-out (>1 arête) = bloc parallel refermé par un join all."""
        lst = outs.get((fid, port)) or []
        if not lst:
            if stop is None:
                return []
            _fail("branche sans issue avant le point de convergence")
        if len(lst) == 1:
            to, eidx = lst[0]
            used.add(eidx)
            return _parse_from(to, stop)
        j = _merge_of([[to] for to, _ in lst])
        if (j is None or nodes[j].get("type") != "join"
                or ((nodes[j].get("params") or {}).get("mode") or "all") != "all"):
            _fail("fan-out sans jointure « all »")
        branches = []
        for to, eidx in lst:
            used.add(eidx)
            branches.append(_parse_from(to, j))
        _consume(j)
        return [{"type": "parallel", "branches": branches}] + _parse_outs(j, 0, stop)

    def _parse_from(nid, stop):
        """Étapes de la séquence commençant à `nid`, jusqu'à `stop` exclu."""
        if nid == stop:
            return []
        node = nodes[nid]
        t = node.get("type")
        if t == "cond":
            _consume(nid)
            heads0 = [to for to, _ in outs.get((nid, 0)) or []]
            heads1 = [to for to, _ in outs.get((nid, 1)) or []]
            m = _merge_of([heads0, heads1]) if heads0 and heads1 else None
            step = {"type": "if", "cond": (node.get("params") or {}).get("cond") or {},
                    "then": _parse_outs(nid, 0, m), "else": _parse_outs(nid, 1, m)}
            return [step] + (_parse_from(m, stop) if m is not None else [])
        if t == "join":
            if ((node.get("params") or {}).get("mode") or "all") != "any":
                _fail("jointure « all » hors d'un parallèle")
            _consume(nid)   # join any en séquence = point de convergence transparent
            return _parse_outs(nid, 0, stop)
        if t in ("entry", "choice"):
            _fail(f"nœud {t} non représentable en blocs")
        _consume(nid)
        step = {"type": t}
        step.update(node.get("params") or {})
        return [step] + _parse_outs(nid, 0, stop)

    _consume(entries[0]["id"])
    blocks = _parse_outs(entries[0]["id"], 0, None)
    if len(consumed) != len(nodes) or len(used) != len(graph.get("edges") or []):
        _fail("nœuds ou arêtes hors de la structure (saut entre branches ?)")
    return blocks


def graph_is_structured(graph):
    """True si le graphe nodes/v2 est représentable en blocs (round-trip sans perte)."""
    try:
        graph_to_blocks(graph)
        return True
    except UnstructuredGraph:
        return False


def macro_format(m):
    """Format du graph stocké d'une macro : 'blocks/v1' (défaut) ou 'nodes/v2'."""
    return ((m or {}).get("graph") or {}).get("format") or "blocks/v1"


def macro_structured(m):
    """True si la macro s'ouvre en vue blocs (blocks/v1, ou nodes/v2 structuré)."""
    if macro_format(m) != GRAPH_FORMAT:
        return True
    return graph_is_structured((m or {}).get("graph") or {})


def run_macro(mid, user=None, allow_system_config=False, entry_id=None):
    """Lance une macro en thread. Un seul run à la fois par macro.
    `allow_system_config` = l'invocateur a containers.deploy (étapes config scope system) —
    capturé au déclenchement, False pour les triggers. `entry_id` (nodes/v2 seulement) =
    démarrer sur UNE entrée précise ; sinon toutes les entrées `manual`.
    Renvoie (run, None) ou (None, "raison")."""
    m = db_get_macro(mid)
    if not m:
        return None, "macro introuvable"
    with _runs_lock:
        cur = _runs.get(mid)
        if cur and cur.running:
            return None, "exécution déjà en cours"
        run = _Run(mid, user, allow_system_config=allow_system_config)
        _runs[mid] = run

    def _go():
        try:
            g = m.get("graph") or {}
            if g.get("format") == "nodes/v2":
                _run_graph(g, m.get("project_id"), run, 0, entry_id=entry_id)
            else:
                _run_steps(g.get("steps"), m.get("project_id"), run, 0)
            run.logline("terminé")
        except Exception as e:
            run.error = str(e)
            run.logline(f"échec : {e}", ok=False)
            db_add_alert("alert.advisory.macro_echec", "warning", kind="advisory",
                         params={"name": m['name'], "e": e})
        finally:
            run.running = False
            run.finished = time.time()

    threading.Thread(target=_go, daemon=True).start()
    return run, None


def run_status(mid):
    with _runs_lock:
        run = _runs.get(mid)
    return run.snapshot() if run else None


def cancel_run(mid):
    with _runs_lock:
        run = _runs.get(mid)
    if run and run.running:
        run._cancel.set()
        return True
    return False


# ─── Déclencheurs permanents (project_triggers, docs/reference/PROJETS.md §7) ─────────────
#
# Poller mutualisé (~1 s) : évalue chaque règle active et lance sa macro sur
# FRONT MONTANT (la condition passe de faux à vrai) — jamais sur niveau — avec
# un cooldown_ms anti-rafale par règle. Une règle qui vient d'apparaître (ou
# d'être ré-activée) est seulement INITIALISÉE au premier passage : activer un
# trigger dont la condition est déjà vraie ne déclenche pas.

TRIGGER_POLL_S = 1.0

_trig_thread = None
_trig_stop = threading.Event()
_trig_prev = {}    # trigger id → dernier état booléen de la condition
_trig_last = {}    # trigger id → time.monotonic() du dernier déclenchement


def _triggers_tick():
    seen = set()
    for tr in db_all_enabled_triggers():
        tid = tr["id"]
        seen.add(tid)
        try:
            pid = tr.get("project_id")
            cur = bool(eval_cond(tr.get("condition") or {}, pid, db_project_vars(pid)))
            prev = _trig_prev.get(tid)
            _trig_prev[tid] = cur
            if prev is None or not cur or prev:
                continue   # premier passage (init), condition fausse, ou pas de front
            now = time.monotonic()
            cd = max(0, int(tr.get("cooldown_ms") or 0)) / 1000.0
            if now - _trig_last.get(tid, float("-inf")) < cd:
                continue
            if not tr.get("macro_id"):
                continue
            _trig_last[tid] = now
            name = tr.get("name") or f"#{tid}"
            run, err = run_macro(tr["macro_id"], user=f"trigger:{name}")
            if err:
                log.debug("trigger %s : macro %s non lancée (%s)", tid, tr["macro_id"], err)
                continue
            m = db_get_macro(tr["macro_id"]) or {}
            db_add_alert("alert.advisory.trigger_declenche", "info", kind="advisory",
                         params={"name": name, "macro": m.get('name') or tr['macro_id']})
        except Exception as e:
            log.debug("trigger %s : évaluation en échec : %s", tid, e)
    # Purge de l'état des règles disparues/désactivées → une ré-activation repart
    # d'une initialisation propre (pas de faux front avec un état périmé).
    for tid in [k for k in _trig_prev if k not in seen]:
        _trig_prev.pop(tid, None)


def start_triggers():
    """Démarre le poller des déclencheurs (thread daemon, ne meurt jamais)."""
    global _trig_thread
    if _trig_thread and _trig_thread.is_alive():
        return
    _trig_stop.clear()

    def _loop():
        while not _trig_stop.wait(TRIGGER_POLL_S):
            try:
                _triggers_tick()
            except Exception as e:   # ceinture ET bretelles : le poller survit à tout
                log.debug("triggers : tick en échec : %s", e)

    _trig_thread = threading.Thread(target=_loop, daemon=True, name="triggers-poller")
    _trig_thread.start()


def stop_triggers():
    global _trig_thread
    _trig_stop.set()
    if _trig_thread:
        _trig_thread.join(timeout=3)
    _trig_thread = None
