# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Règles DÉCLARÉES du journal d'exploitation (`control.journal` du plugin.json) — fonctions PURES.

Aucune E/S ici : ni base, ni réseau, ni Flask. Ce module dit, pour une commande donnée, CE QUI
mérite une ligne et SOUS QUEL NOM ; `app/ops_journal.py` écrit. Spécification complète :
docs/reference/PLUGIN_JOURNAL.md.

★ LA RÈGLE QUI DÉCIDE DE TOUT : LE TYPE DE LA VALEUR. Un booléen ou une chaîne est un geste
DISCRET (mute, on/off d'un départ, bypass, mode d'un filtre) : il est noté. Un nombre est un
geste CONTINU (fader, pan, trim, niveau de départ, bouton d'EQ) : il ne l'est JAMAIS, pas même
résumé — un ingé son garde le doigt sur un fader pendant une heure (décision de l'exploitant,
2026-10-17). Un objet ou une liste est rejeté aussi : il porte presque toujours des nombres, et
le journal n'a pas à deviner lesquels comptent. Règle simple, donc impossible à contourner par
un plugin distrait : il n'y a rien à déclarer pour exclure un fader.

★ NOMS LISIBLES ET IDS, LES DEUX. Un chemin `strips.s_in1.chain.e_mute1.params.on` devient
« Entrée 1 · mute » à partir du document PERSISTÉ (celui de `control.persist`) ; le chemin d'ids
est gardé à côté dans la ligne : un libellé se renomme, un id non.
"""
import re

MODES = ("set", "fields", "action", "structure")
# Vocabulaire FERMÉ des types d'action (filtre « type » de la page). Un plugin choisit le sien par
# endpoint ; à défaut, le mode le donne (set → setting, fields → command, structure → structure).
CATEGORIES = ("setting", "mute", "command", "structure", "event")
_DEFAULT_CATEGORY = {"set": "setting", "fields": "command", "action": "command", "structure": "structure"}

DEFAULT_LABEL_FIELDS = ("label", "name", "short")
DEFAULT_KIND_FIELDS = ("type", "kind")
DEFAULT_QUIET_KEYS = ("params",)
DEFAULT_IGNORE_KEYS = ("version",)
MAX_DIFF = 40            # au-delà, la ligne de structure dit « +N » (le détail reste borné)
SEP = " · "


# ─── Déclaration ──────────────────────────────────────────────────────────────────────────────

def _post_endpoints(ctrl):
    eps = ctrl.get("endpoints") or []
    if isinstance(eps, dict):
        return {(s or {}).get("path") or ("/" + k) for k, s in eps.items()}
    return set(eps)


def validate_spec(manifest):
    """Erreurs de `control.journal` (liste vide = valide OU absente : un plugin qui ne déclare rien
    n'est jamais refusé pour ça)."""
    ctrl = (manifest or {}).get("control") or {}
    j = ctrl.get("journal")
    if j is None:
        return []
    if not isinstance(j, dict):
        return ["control.journal doit être un objet"]
    errs = []
    eps = _post_endpoints(ctrl)
    reads = set(ctrl.get("read_endpoints") or [])
    persist = ctrl.get("persist") if isinstance(ctrl.get("persist"), dict) else {}
    apply_ep = (persist.get("structure") or {}).get("apply") if isinstance(persist.get("structure"), dict) else None
    for i, pat in enumerate(j.get("exclude") or []):
        try:
            re.compile(pat)
        except (re.error, TypeError) as e:
            errs.append("control.journal.exclude[%d] : motif invalide (%s)" % (i, e))
    if not isinstance(j.get("exclude", []), list):
        errs.append("control.journal.exclude doit être une liste de motifs")
    names = j.get("names", {})
    if not isinstance(names, dict):
        errs.append("control.journal.names doit être un objet")
    elif "param" in names and not (isinstance(names["param"], str) and names["param"]):
        errs.append("control.journal.names.param : nom de clé de params requis")
    for k in ("label_fields", "kind_fields", "ref_fields", "quiet_keys", "ignore_keys"):
        if isinstance(names, dict) and k in names and not (
                isinstance(names[k], list) and all(isinstance(x, str) for x in names[k])):
            errs.append("control.journal.names.%s : liste de chaînes attendue" % k)
    for k in ("kind_names", "key_names"):
        if isinstance(names, dict) and k in names and not isinstance(names[k], dict):
            errs.append("control.journal.names.%s : objet attendu" % k)
    endpoints = j.get("endpoints")
    if not isinstance(endpoints, dict):
        errs.append("control.journal.endpoints : objet {endpoint: règle | false} requis")
        endpoints = {}
    for ep, rule in endpoints.items():
        where = "control.journal.endpoints[%s]" % ep
        if ep not in eps:
            errs.append("%s : n'est pas dans control.endpoints" % where)
        if rule is False or rule is None:
            continue
        if not isinstance(rule, dict):
            errs.append("%s : objet ou false attendu" % where)
            continue
        mode = rule.get("mode")
        if mode not in MODES:
            errs.append("%s.mode : un de %s" % (where, ", ".join(MODES)))
            continue
        cat = rule.get("category")
        if cat is not None and cat not in CATEGORIES:
            errs.append("%s.category : un de %s" % (where, ", ".join(CATEGORIES)))
        if mode == "set":
            for k in ("path_field", "value_field", "batch"):
                if k in rule and not (isinstance(rule[k], str) and rule[k]):
                    errs.append("%s.%s : chaîne non vide attendue" % (where, k))
        elif mode in ("fields", "action"):
            f = rule.get("fields", {} if mode == "action" else None)
            if not (isinstance(f, dict) and (f or mode == "action")
                    and all(isinstance(k, str) and isinstance(v, str) for k, v in f.items())):
                errs.append("%s.fields : objet {champ du corps: libellé} non vide requis" % where)
                f = {}
            if mode == "action" and not (isinstance(rule.get("label"), str) and rule["label"].strip()):
                errs.append("%s.label : libellé fixe de l'action requis" % where)
            if "target" in rule and not (isinstance(rule["target"], str) and rule["target"]):
                errs.append("%s.target : nom de champ attendu" % where)
            vr = rule.get("value_ref", {})
            if not isinstance(vr, dict):
                errs.append("%s.value_ref : objet {champ: référence} attendu" % where)
                vr = {}
            for fld, ref in vr.items():
                if fld not in f:
                    errs.append("%s.value_ref[%s] : champ absent de fields" % (where, fld))
                if not _valid_ref(ref):
                    errs.append("%s.value_ref[%s] : \"<collection>\", {collection, match} ou {path: true}"
                                % (where, fld))
            sw = rule.get("skip_when", {})
            if not (isinstance(sw, dict) and all(isinstance(k, str) for k in sw)):
                errs.append("%s.skip_when : objet {champ: valeur} attendu" % where)
        elif mode == "structure":
            # Le diff exige l'ancien document : seul `control.persist` le garde AVANT l'application.
            if not apply_ep or ep != apply_ep:
                errs.append("%s : mode structure réservé à control.persist.structure.apply" % where)
    ev = j.get("events")
    if ev is not None:
        if not isinstance(ev, dict) or ev.get("endpoint") not in reads:
            errs.append("control.journal.events.endpoint doit être un des read_endpoints")
        elif "rev_field" in ev and not (isinstance(ev["rev_field"], str) and ev["rev_field"]):
            errs.append("control.journal.events.rev_field : chaîne attendue")
    return errs


def _valid_ref(ref):
    if isinstance(ref, str):
        return bool(ref)
    if isinstance(ref, dict):
        if ref.get("path") is True and len(ref) == 1:
            return True
        return isinstance(ref.get("collection"), str) and bool(ref["collection"]) and \
            isinstance(ref.get("match", "id"), str)
    return False


def spec(manifest):
    """Déclaration normalisée, ou None (absente ou invalide — le scan l'a déjà refusée)."""
    ctrl = (manifest or {}).get("control") or {}
    j = ctrl.get("journal")
    if not isinstance(j, dict) or validate_spec(manifest):
        return None
    names = dict(j.get("names") or {})
    persist = ctrl.get("persist") if isinstance(ctrl.get("persist"), dict) else {}
    names.setdefault("param", persist.get("param"))
    eps = {}
    for ep, rule in (j.get("endpoints") or {}).items():
        if not isinstance(rule, dict):
            continue
        r = dict(rule)
        r.setdefault("category", _DEFAULT_CATEGORY[r["mode"]])
        if r["mode"] == "set":
            r.setdefault("path_field", "path")
            r.setdefault("value_field", "value")
        eps[ep] = r
    return {"endpoints": eps,
            "exclude": [re.compile(p) for p in (j.get("exclude") or [])],
            "names": names,
            "events": j.get("events") if isinstance(j.get("events"), dict) else None}


# ─── Valeurs ──────────────────────────────────────────────────────────────────────────────────

def is_discrete(v):
    """Booléen ou chaîne : oui. Tout le reste (nombre, objet, liste, null) : non.
    ⚠ `bool` est une sous-classe d'`int` en Python : le test du booléen passe EN PREMIER."""
    if isinstance(v, bool):
        return True
    return isinstance(v, str)


def excluded(path, sp):
    return any(p.search(path or "") for p in (sp or {}).get("exclude") or [])


def extract(rule, body, sp):
    """Entrées journalisables d'un corps de POST : [{path, value}] (sans libellé). Pur et BON
    MARCHÉ : c'est le chemin chaud d'un fader (des dizaines de POST par seconde), qui doit sortir
    ici sans avoir lu la base."""
    if not isinstance(body, dict) or not rule:
        return []
    out = []
    if rule["mode"] == "set":
        batch = rule.get("batch")
        items = body.get(batch) if batch and isinstance(body.get(batch), list) else [body]
        for it in items:
            if not isinstance(it, dict):
                continue
            path, value = it.get(rule["path_field"]), it.get(rule["value_field"])
            if not isinstance(path, str) or not path or not is_discrete(value) or excluded(path, sp):
                continue
            out.append({"path": path, "value": value})
    elif rule["mode"] in ("fields", "action"):
        # `skip_when` : une commande à BLANC (`dry_run`) n'a pas eu lieu — rien n'est noté.
        sw = rule.get("skip_when") or {}
        if sw and all(_get(body, k) == v for k, v in sw.items()):
            return []
        tgt_field = rule.get("target")
        target = _get(body, tgt_field) if tgt_field else None
        target = str(target) if isinstance(target, (str, int)) and not isinstance(target, bool) \
            and target != "" else None
        action = rule["mode"] == "action"
        if action:                                  # la commande elle-même : TOUJOURS une ligne
            out.append({"path": target or "", "value": None, "target": target,
                        "field_label": rule["label"], "head": True})
        refs = rule.get("value_ref") or {}
        for field, flabel in (rule.get("fields") or {}).items():
            v = _get(body, field)
            ref = refs.get(field)
            if not (is_discrete(v) or (ref and _is_ref_value(v))):
                continue
            path = "%s.%s" % (target, field) if target else field
            if excluded(path, sp):
                continue
            e = {"path": path, "value": v, "target": None if action else target, "field_label": flabel}
            if ref:
                e["ref"] = ref
            if action:
                e["sub"] = True
            out.append(e)
    return out


def _get(body, field):
    """Champ d'un corps, chemin pointé admis (`tool.type`)."""
    v = body
    for part in str(field or "").split("."):
        if not isinstance(v, dict):
            return None
        v = v.get(part)
    return v


def _is_ref_value(v):
    """Une RÉFÉRENCE (id, numéro, chemin, ou liste de ceux-ci) n'est pas un geste continu : un numéro de
    scène désigne une scène, il ne se règle pas. Admise SEULEMENT quand la règle déclare `value_ref`."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (str, int)):
        return True
    return isinstance(v, list) and 0 < len(v) <= 256 and all(
        isinstance(x, (str, int)) and not isinstance(x, bool) for x in v)


# ─── Noms lisibles ────────────────────────────────────────────────────────────────────────────

def _cfg(sp):
    n = (sp or {}).get("names") or {}
    return {"label_fields": tuple(n.get("label_fields") or DEFAULT_LABEL_FIELDS),
            "kind_fields": tuple(n.get("kind_fields") or DEFAULT_KIND_FIELDS),
            "ref_fields": tuple(n.get("ref_fields") or ()),
            "quiet_keys": set(n.get("quiet_keys") or DEFAULT_QUIET_KEYS),
            "ignore_keys": set(n.get("ignore_keys") or DEFAULT_IGNORE_KEYS),
            "kind_names": dict(n.get("kind_names") or {}),
            "key_names": dict(n.get("key_names") or {})}


def id_index(doc):
    """{id: (élément, clé de la collection)} de tous les objets du document qui portent un `id`."""
    idx = {}

    def walk(o, coll):
        if isinstance(o, dict):
            if isinstance(o.get("id"), str) and o["id"] not in idx:
                idx[o["id"]] = (o, coll)
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for x in o:
                walk(x, coll)
    walk(doc, None)
    return idx


def element_name(el, coll, idx, cfg):
    """Nom d'un élément : son libellé, à défaut sa nature (`type` avant `kind` : un module de filtre
    est un « filter » avant d'être un « module »), à défaut le nom de sa collection, à défaut l'id.
    Un champ de référence (`bus` d'un départ) ajoute « → <nom de la cible> »."""
    name = None
    for f in cfg["label_fields"]:
        v = el.get(f)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    if name is None:
        for f in cfg["kind_fields"]:
            v = el.get(f)
            if isinstance(v, str) and v:
                name = cfg["kind_names"].get(v, v)
                break
    ref = None
    for f in cfg["ref_fields"]:
        v = el.get(f)
        if isinstance(v, str) and v in idx and v != el.get("id"):
            ref = ref_name(v, idx, cfg)
            break
    if name is None:
        name = cfg["key_names"].get(coll) or (coll if ref else None) or str(el.get("id"))
    return "%s → %s" % (name, ref) if ref else name


def ref_name(ident, idx, cfg):
    hit = idx.get(ident)
    if not hit:
        return ident
    el, coll = hit
    for f in cfg["label_fields"]:
        v = el.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return element_name(dict(el, **{f: None for f in cfg["ref_fields"]}), coll, {}, cfg)


def resolve_label(doc, path, sp, idx=None):
    """Chemin d'ids → nom lisible. Repli : le segment tel quel (jamais d'échec, jamais vide)."""
    cfg = _cfg(sp)
    doc = doc if isinstance(doc, dict) else {}
    idx = id_index(doc) if idx is None else idx
    segs = [s for s in str(path or "").split(".") if s != ""]
    parts, node, coll = [], doc, None
    i = 0
    if segs and segs[0] not in doc and segs[0] in idx:     # chemin qui commence par un id (/mute {id})
        node, coll = idx[segs[0]]
        parts.append(element_name(node, coll, idx, cfg))
        i = 1
    for seg in segs[i:]:
        if isinstance(node, list):
            el = next((x for x in node if isinstance(x, dict) and x.get("id") == seg), None)
            if el is None and seg.isdigit() and int(seg) < len(node) and isinstance(node[int(seg)], dict):
                el = node[int(seg)]
            if el is not None:
                parts.append(element_name(el, coll, idx, cfg))
                node = el
                continue
            parts.append(cfg["key_names"].get(seg, seg))
            node = None
            continue
        if isinstance(node, dict) and seg in node:
            child = node[seg]
            if isinstance(child, list):
                node, coll = child, seg                         # collection : son nom ne s'écrit pas
                continue
            if seg not in cfg["quiet_keys"]:
                parts.append(cfg["key_names"].get(seg, seg))
            node = child if isinstance(child, dict) else None
            continue
        if seg in idx:                                           # dict indexé par ids (`solo.<id>`)
            parts.append(ref_name(seg, idx, cfg))
            node = None
            continue
        # Clé ABSENTE du document (alias du moteur, `listen_mode`) : `key_names` s'applique quand même.
        if seg not in cfg["quiet_keys"]:
            parts.append(cfg["key_names"].get(seg, seg))
        node = None
    return SEP.join(parts) if parts else str(path or "")


def resolve_ref(doc, value, ref, sp, idx=None):
    """Valeur-référence → nom lisible, d'après le document persisté. Repli : la valeur telle quelle.
    `ref` = "<collection>" (correspondance sur `id`), {collection, match} (ex. `number`), ou
    {path: true} (la valeur est un chemin d'ids du document)."""
    doc = doc if isinstance(doc, dict) else {}
    idx = id_index(doc) if idx is None else idx
    if isinstance(value, list):
        return ", ".join(str(resolve_ref(doc, x, ref, sp, idx)) for x in value)
    if isinstance(ref, dict) and ref.get("path") is True:
        return resolve_label(doc, str(value), sp, idx)
    coll, match = (ref, "id") if isinstance(ref, str) else (ref.get("collection"), ref.get("match", "id"))
    node = _get(doc, coll)
    if isinstance(node, list):
        el = next((x for x in node if isinstance(x, dict) and x.get(match) is not None
                   and str(x.get(match)) == str(value)), None)
        if el is not None:
            return element_name(el, coll.rsplit(".", 1)[-1], idx, _cfg(sp))
    return value


def label_entries(entries, doc, sp):
    """Ajoute `label` à chaque entrée extraite (index du document construit UNE fois), et `display`
    (nom lisible) à une valeur-référence."""
    idx = id_index(doc if isinstance(doc, dict) else {})
    out = []
    for e in entries:
        e = dict(e)
        ref = e.pop("ref", None)
        if ref is not None:
            e["display"] = resolve_ref(doc, e.get("value"), ref, sp, idx)
        if "field_label" in e:
            tgt = e.get("target")
            head = ref_name(tgt, idx, _cfg(sp)) if tgt else None
            e["label"] = SEP.join(x for x in (head, e.pop("field_label")) if x)
            e.pop("target", None)
        else:
            e["label"] = resolve_label(doc, e["path"], sp, idx)
        out.append(e)
    return out


# ─── Différences de structure ─────────────────────────────────────────────────────────────────

def _is_id_list(lst):
    return isinstance(lst, list) and all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in lst)


def _disp(v, idx, cfg):
    """Valeur discrète d'un diff, montrée lisible : un id connu devient son nom."""
    if v is None:
        return "—"
    if isinstance(v, str) and v in idx:
        return ref_name(v, idx, cfg)
    return v


def diff_docs(old, new, sp):
    """Résumé des différences DISCRÈTES entre deux documents. → (entrées, nombre omis).

    Générique (aucun nom de collection n'est codé ici) : une liste d'objets à `id` est une
    collection — éléments ajoutés, retirés, renommés ; les autres scalaires ne sont comparés que
    s'ils sont discrets (affectations `vca`/`automix`/`src`, `bypass`, `on`, modes) ; une liste
    sans ids (patch, canaux, `route_to`) est comparée en bloc (« modifié ») ou, si c'est une liste
    d'ids, en ajouts/retraits nommés. Les nombres ne sont JAMAIS comparés : un niveau de fader
    qui diffère entre l'instantané persisté et le nouveau document n'est pas un geste de structure.
    Les motifs `exclude` s'appliquent (sélection PFL)."""
    cfg = _cfg(sp)
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    idx_old, idx_new = id_index(old), id_index(new)
    out = []

    def lab(path, removed=False):
        return resolve_label(old if removed else new, path, sp, idx_old if removed else idx_new)

    def add(entry):
        if excluded(entry["path"], sp):
            return
        out.append(entry)

    def walk(a, b, path, in_element):
        p = ".".join(path)
        if isinstance(a, dict) and isinstance(b, dict):
            for k in list(a.keys()) + [k for k in b.keys() if k not in a]:
                if k in cfg["ignore_keys"] or k == "id":
                    continue
                if in_element and k in cfg["label_fields"]:
                    continue                                    # couvert par « renommé »
                walk(a.get(k), b.get(k), path + [k], False)
            return
        if _is_id_list(a) and _is_id_list(b) and (a or b):
            ma = {x["id"]: x for x in a}
            mb = {x["id"]: x for x in b}
            for i in mb:
                if i not in ma:
                    add({"op": "added", "path": ".".join(path + [i]), "label": lab(".".join(path + [i]))})
            for i in ma:
                if i not in mb:
                    add({"op": "removed", "path": ".".join(path + [i]),
                         "label": lab(".".join(path + [i]), removed=True)})
            for i in ma:
                if i in mb:
                    ea, eb = ma[i], mb[i]
                    # Renommage = le PREMIER champ de libellé déclaré (`label` par défaut) ; le nom
                    # court et les autres ne font pas une ligne à eux seuls.
                    lf = cfg["label_fields"][0] if cfg["label_fields"] else "label"
                    if (ea.get(lf) or "") != (eb.get(lf) or ""):
                        add({"op": "renamed", "path": ".".join(path + [i]),
                             "from": ea.get(lf) or "", "to": eb.get(lf) or ""})
                    walk(ea, eb, path + [i], True)
            return
        if isinstance(a, list) or isinstance(b, list):
            if a == b or (a in (None, []) and b in (None, [])):
                return                                          # liste vide apparue (champ migré) : rien de fait
            if isinstance(a, list) and isinstance(b, list) and all(isinstance(x, str) for x in a + b):
                plus = [x for x in b if x not in a]
                moins = [x for x in a if x not in b]
                for x in plus:
                    add({"op": "changed", "path": p, "label": lab(p), "from": None, "to": "+" + str(_disp(x, idx_new, cfg))})
                for x in moins:
                    add({"op": "changed", "path": p, "label": lab(p), "from": None, "to": "−" + str(_disp(x, idx_old, cfg))})
                return
            add({"op": "changed", "path": p, "label": lab(p)})
            return
        if isinstance(a, dict) or isinstance(b, dict):          # sous-objet apparu / disparu
            if a is None:
                add({"op": "added", "path": p, "label": lab(p)})
            elif b is None:
                add({"op": "removed", "path": p, "label": lab(p, removed=True)})
            else:
                add({"op": "changed", "path": p, "label": lab(p)})
            return
        if a == b:
            return
        # Scalaires : discrets seulement (None compte pour « rien » : vca None → v_1 est une affectation).
        if (a is None or is_discrete(a)) and (b is None or is_discrete(b)) and not (a is None and b is None):
            add({"op": "changed", "path": p, "label": lab(p, removed=b is None),
                 "from": _disp(a, idx_old, cfg), "to": _disp(b, idx_new, cfg)})

    walk(old, new, [], False)
    omitted = max(0, len(out) - MAX_DIFF)
    return out[:MAX_DIFF], omitted
