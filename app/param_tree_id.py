"""Macro parameter tree derived from a persisted DOCUMENT and keyed by ID.

`app/macros.py:param_tree` builds positional elements (`box:{i}`) from the manifest + live caps.
For a plugin whose structure is ONE JSON document (audio_console: strips, buses, chains) a
positional path breaks the day a strip is inserted: every macro written before points at the
wrong thing. Here the manifest declares where the document lives in `deploy_config.params` and
how to walk it; every settable leaf carries an ID path (`strips.s_in1.chain.e_fad1.params.gain_db`)
that does not move when siblings are added, removed or reordered (ids are stable, never reused).

The persisted document is the source (works with the container stopped); bounds come from the
catalog file shipped with the plugin (`catalog`, e.g. modules.json) — never invented here.

Manifest block (`param_tree.by_id`):
  document      key of the document in deploy_config.params (fallback: deploy_defaults)
  catalog       JSON file in the plugin directory: {elements:{kind:{params}}, modules:{type:{params}},
                fields:{name:{spec}}}; a param spec is {type,min,max,step,unit,default,options}
                or {type:"group", params:{…}} (flattened into « group · param »)
  set           {endpoint, path_key, value_key} : how a leaf is POSTed ({path:…, value:…})
  read_endpoint endpoint where the same path is readable (it resolves lists BY ID, cf. _walk_path)
  kind_labels   {kind|module type: human label}
  map_caps      {map param name: /state.caps family} — a `map` param (keys known only to the running instance,
                e.g. third-party plugin settings) gets one leaf per key from caps[family][item id] =
                {label, params:{key: catalog-like spec}}; label replaces the kind label. No caps -> no leaf
  roots         [node] — node = {list, kind, label, label_field, children:[child]}
  root          node with `object:"talkback"` instead of `list` = a single object at the document root
  child         {fields:{pfl:{field,label,live,always}}, label}  scalar fields of the enclosing object
                (`live`: read endpoint override, e.g. "/doc?live=1" for momentary states;
                `always`: leaf even when the key is absent from the persisted document)
                {chain:"chain", label:"Chaîne"}                    list of elements/modules
                {list:"sends", label:…, field:{param:"catalog.fields key"}, field_labels:{param:label}, ref:{list,field},
                 key_field:"bus", field_skip_ref:{param:{ref_field:[values]}}}   (key_field: item field used
                as path id, default "id"; field_skip_ref: no leaf for `param` when the REFERENCED object's
                field has one of these values — e.g. no level on a send that is an assignment to a master)
                {object:"mixminus_out", children:[child…]}         optional sub-object
                {list:"direct_outs", children:[child…], label_field:"label"}   list of sub-objects keyed by
                id, each walked like an object (path <prefix>.direct_outs.<id>.…)
"""
import json
import os

_CATALOG_CACHE = {}


def _load_catalog(plugin_dir, name):
    if not name or not plugin_dir:
        return {}
    path = os.path.join(plugin_dir, name)
    try:
        mt = os.path.getmtime(path)
        hit = _CATALOG_CACHE.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        with open(path, encoding="utf-8") as f:
            cat = json.load(f)
        _CATALOG_CACHE[path] = (mt, cat)
        return cat
    except Exception:
        return {}


def get_document(spec, params, manifest):
    """The persisted document: deploy_config.params[document], else deploy_defaults."""
    key = spec.get("document")
    doc = (params or {}).get(key)
    if not isinstance(doc, dict):
        doc = ((manifest.get("deploy_defaults") or {}).get(key))
    return doc if isinstance(doc, dict) else {}


def _bounds(ps):
    """Leaf fields from a catalog param spec; None if it is not a settable scalar."""
    t = ps.get("type")
    if t == "bool":
        return {"type": "bool", "default": bool(ps.get("default", False))}
    if t == "enum":
        opts = ps.get("options")
        if not opts:
            return None
        return {"type": "enum", "options": list(opts), "default": ps.get("default", opts[0])}
    if t in ("float", "int"):
        mn, mx = ps.get("min"), ps.get("max")
        if mn is None or mx is None:
            return None          # no declared bounds → skipped, never guessed
        out = {"type": "number", "min": mn, "max": mx,
               "default": ps.get("default", mn),
               "step": ps.get("step", 1 if t == "int" else 0.01)}
        if ps.get("unit"):
            out["unit"] = ps["unit"]
        return out
    return None


def _flatten(pspecs, values, prefix=()):
    """(subpath tuple, spec, current value) over a catalog params dict (groups flattened)."""
    for k, ps in (pspecs or {}).items():
        if not isinstance(ps, dict):
            continue
        cur = (values or {}).get(k) if isinstance(values, dict) else None
        if ps.get("type") == "group":
            yield from _flatten(ps.get("params"), cur, prefix + (k,))
        else:
            yield prefix + (k,), ps, cur


def _leaf(spec, label, path, ps, cur, read_endpoint=None):
    b = _bounds(ps)
    if b is None:
        return None
    sc = spec.get("set") or {}
    if cur is not None and not isinstance(cur, (dict, list)):
        b["default"] = cur
    leaf = {"key": sc.get("value_key") or "value", "label": label, "endpoint": sc.get("endpoint"),
            "wrap": None, "idx": None, "path": path, "id": path,
            "read_endpoint": read_endpoint or spec.get("read_endpoint"),
            "fixed": {sc.get("path_key") or "path": path}}
    leaf.update(b)
    return leaf


def _kind_label(spec, cat, item):
    typ = item.get("type") if item.get("kind") == "module" else item.get("kind")
    kl = spec.get("kind_labels") or {}
    return kl.get(typ) or (cat.get("labels") or {}).get(typ) or str(typ)


def _chain_groups(spec, cat, prefix, chain, caps=None):
    groups, seen = [], {}
    items = [i for i in (chain or []) if isinstance(i, dict) and i.get("id")]
    for it in items:
        seen[_kind_label(spec, cat, it)] = seen.get(_kind_label(spec, cat, it), 0) + 1
    for it in items:
        base = _kind_label(spec, cat, it)
        label = base if seen[base] == 1 else "%s (%s)" % (base, it["id"])
        if it.get("kind") == "module":
            pspecs = ((cat.get("modules") or {}).get(it.get("type")) or {}).get("params")
        else:
            pspecs = ((cat.get("elements") or {}).get(it.get("kind")) or {}).get("params")
        ipath = "%s.chain.%s.params" % (prefix, it["id"])
        params = []
        for sub, ps, cur in _flatten(pspecs, it.get("params")):
            if ps.get("type") == "map":
                # keys known only to the running instance (third-party plugin settings): bounds from
                # caps[map_caps[param]][item id] = {label, params: {key: catalog-like spec}}. No caps (instance
                # off, plugin not described yet) -> no leaf: never guessed.
                fam = (spec.get("map_caps") or {}).get(sub[-1])
                ent = ((caps or {}).get(fam) or {}).get(it["id"]) if fam else None
                if isinstance(ent, dict):
                    if ent.get("label"):
                        label = ent["label"] if seen[base] == 1 else "%s (%s)" % (ent["label"], it["id"])
                    for k, kps in (ent.get("params") or {}).items():
                        if isinstance(kps, dict):
                            lf = _leaf(spec, kps.get("label") or k, "%s.%s.%s" % (ipath, ".".join(sub), k), kps,
                                       (cur or {}).get(k) if isinstance(cur, dict) else None)
                            if lf:
                                params.append(lf)
                continue
            lf = _leaf(spec, " · ".join(sub), ipath + "." + ".".join(sub), ps, cur)
            if lf:
                params.append(lf)
        if params:
            groups.append({"label": label, "params": params})
    return groups


def _children(spec, cat, doc, prefix, obj, children, caps=None):
    groups = []
    for ch in children or []:
        if ch.get("chain"):
            groups += _chain_groups(spec, cat, prefix, (obj or {}).get(ch["chain"]), caps)
        elif ch.get("list") and ch.get("children"):
            for it in (obj or {}).get(ch["list"]) or []:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                name = "%s %s" % (ch.get("label") or ch["list"], it.get(ch.get("label_field") or "label") or it["id"])
                for g in _children(spec, cat, doc, "%s.%s.%s" % (prefix, ch["list"], it["id"]), it,
                                   ch.get("children"), caps):
                    g["label"] = "%s › %s" % (name, g["label"])
                    groups.append(g)
        elif ch.get("list"):
            ref = ch.get("ref") or {}
            params = []
            for it in (obj or {}).get(ch["list"]) or []:
                kid = it.get(ch.get("key_field") or "id")   # sources: keyed by their bus
                if not isinstance(it, dict) or not kid:
                    continue
                tgt = it.get(ref.get("field")) if ref else None
                ro = next((x for x in doc.get(ref.get("list"), []) if isinstance(x, dict) and x.get("id") == tgt),
                          None) if ref else None
                tl = (ro.get("label") or ro.get("id")) if ro else (tgt if ref else it["id"])
                flds = ch.get("field") or {}
                skip = ch.get("field_skip_ref") or {}
                for pk, fk in flds.items():
                    ps = (cat.get("fields") or {}).get(fk)
                    if not ps:
                        continue
                    if ro and any(ro.get(f) in vals for f, vals in (skip.get(pk) or {}).items()):
                        continue
                    ipath = "%s.%s.%s.%s" % (prefix, ch["list"], kid, pk)
                    sfx = (ch.get("field_labels") or {}).get(pk, pk) if len(flds) > 1 else ""
                    lf = _leaf(spec, "→ %s%s" % (tl, " · " + sfx if sfx else ""), ipath, ps,
                               it.get(pk))
                    if lf:
                        params.append(lf)
            if params:
                groups.append({"label": ch.get("label") or ch["list"], "params": params})
        elif ch.get("fields"):
            # scalar fields on the enclosing object itself: {"pfl": {"field": "pfl", "label": "PFL",
            # "live": "/doc?live=1", "always": true, "kinds": [...]}}. Present-in-document only unless `always`
            # (`kinds` then limits it to those object kinds)
            # (a bus kind without PFL has no `pfl` key). `live` = read endpoint of a momentary
            # state that the persisted document does not carry.
            params = []
            for pk, fd in ch["fields"].items():
                fd = fd if isinstance(fd, dict) else {"field": fd}
                ps = (cat.get("fields") or {}).get(fd.get("field") or pk)
                if not ps or (pk not in (obj or {}) and not fd.get("always")):
                    continue
                if fd.get("kinds") and (obj or {}).get("kind") not in fd["kinds"]:
                    continue                  # `kinds` : seulement ces genres d'objet (bus à PFL…)
                lf = _leaf(spec, fd.get("label") or pk, "%s.%s" % (prefix, pk), ps,
                           (obj or {}).get(pk), fd.get("live"))
                if lf:
                    params.append(lf)
            if params:
                groups.append({"label": ch.get("label") or "", "params": params})
        elif ch.get("object"):
            sub = (obj or {}).get(ch["object"])
            if isinstance(sub, dict):
                for g in _children(spec, cat, doc, "%s.%s" % (prefix, ch["object"]), sub,
                                   ch.get("children"), caps):
                    g["label"] = "%s › %s" % (ch.get("label") or ch["object"], g["label"])
                    groups.append(g)
    return groups


def build_elements(spec, manifest, params, caps=None):
    """Resolved elements [{id,label,kind,idx,groups}] from the persisted document. `caps` = the live /state.caps
    (optional): bounds of `map` params listed in `map_caps`."""
    cat = _load_catalog(manifest.get("_dir"), spec.get("catalog"))
    doc = get_document(spec, params, manifest)
    out = []
    for node in spec.get("roots") or []:
        if node.get("object"):      # a single object at the document root (talkback)
            o = doc.get(node["object"])
            objs = [(node["object"], dict(o, id=node["object"]))] if isinstance(o, dict) else []
        else:
            objs = [("%s.%s" % (node["list"], x["id"]), x) for x in doc.get(node["list"]) or []
                    if isinstance(x, dict) and x.get("id")]
        for pre, obj in objs:
            groups = _children(spec, cat, doc, pre, obj, node.get("children"), caps)
            if groups:
                label = obj.get(node.get("label_field") or "label") or obj["id"]
                kind = node.get("kind") or node.get("list") or node["object"]
                out.append({"id": "%s:%s" % (kind, obj["id"]), "kind": kind, "idx": None,
                            "label": node.get("label") if node.get("object") else
                            ("%s — %s" % (node["label"], label) if node.get("label") else label),
                            "groups": groups})
    return out
