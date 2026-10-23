"""Câblage DÉCLARATIF d'un plugin dont les ports vivent dans un DOCUMENT JSON (console audio…).

Le câblage historique (`from_list`) déplie une liste de premier niveau de `deploy_config.params`,
sans filtre, avec pour slot l'indice dans la liste. Un plugin « document » (une seule clé
`params.console` portant tranches, bus, chaînes) a besoin de plus, et l'orchestrateur n'a pas à
connaître le plugin pour le lui donner. Le manifeste déclare, par entrée de `wiring.produces` /
`wiring.consumes` :

    "from_list": "console.buses"          chemin POINTÉ vers la liste (la racine = clé de params)
    "where":     {"output": true, "kind": {"ne": "mixminus"}}
                                          filtre sur l'élément (champs pointés ; valeur nue = égalité ;
                                          {"ne": v} {"present": bool} {"truthy": bool} {"in": [..]})
    "key":       "{id}/direct_out"        clé STABLE du port (gabarit sur les champs de l'élément).
                                          Elle devient le `slot` : un id survit à l'insertion d'une
                                          tranche, un indice non (et les câbles enregistrés visent le slot)
    "shm":       "{hostname}_{key_flat}"  nom du flux PRODUIT (key_flat = key avec « / » → « _ ») ; pour une
                                          ENTRÉE, `shm_field` (chemin pointé dans l'élément) porte le flux câblé
    "label":     "{label} (direct)"       gabarit de libellé sur les champs de l'élément

Une entrée sans `key` reste traitée par l'ancien code (listes de premier niveau, slot = indice).
Ce module est PUR pour tout ce qui dérive (expand/with_field) ; seule `wire` touche la base et le
conteneur, sous `verrou_vmid`, en relisant la config FRAÎCHE.
"""
import copy
import json
import re

import requests

HTTP_TIMEOUT_S = 5.0
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def get_path(obj, path, default=None):
    """`get_path(params, "console.buses")` — chemin pointé dans dicts imbriqués."""
    cur = obj
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(obj, path, value):
    """Écrit `value` au chemin pointé de `obj` (crée les dicts manquants). Mute `obj`."""
    parts = str(path).split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = cur[part] = {}
        cur = nxt
    cur[parts[-1]] = value


def _cond(val, cond):
    if isinstance(cond, dict):
        for op, ref in cond.items():
            if op == "ne" and not val != ref:
                return False
            if op == "present" and (val is not None) != bool(ref):
                return False
            if op == "truthy" and bool(val) != bool(ref):
                return False
            if op == "in" and val not in (ref or []):
                return False
            if op not in ("ne", "present", "truthy", "in"):
                return False          # opérateur inconnu : refuser, jamais laisser passer
        return True
    return val == cond


def match(entry, where):
    """L'élément satisfait-il TOUTES les conditions de `where` ?"""
    return all(_cond(get_path(entry, f), c) for f, c in (where or {}).items())


def fill(tpl, entry, **extra):
    """Substitue `{champ}` (champs de l'élément, pointés permis) et `extra`. Champ absent → « »."""
    if not isinstance(tpl, str):
        return tpl

    def rep(m):
        name = m.group(1)
        if name in extra:
            return str(extra[name])
        v = get_path(entry, name)
        return "" if v is None or isinstance(v, (dict, list)) else str(v)
    return _PLACEHOLDER.sub(rep, tpl)


def expand(spec, params, hostname=""):
    """[(key, élément)] d'une entrée de wiring en mode clé, dans l'ordre du document. Clés vides
    ou en double : l'élément est ignoré (une clé ambiguë ne désignerait pas un seul port)."""
    lst = get_path(params or {}, spec.get("from_list") or "")
    if isinstance(lst, dict):        # objet unique (ex. `console.talkback`) : un seul port
        lst = [lst]
    out, seen = [], set()
    for entry in lst if isinstance(lst, list) else []:
        if not isinstance(entry, dict) or not match(entry, spec.get("where")):
            continue
        key = fill(spec.get("key_tpl") or spec["key"], entry, hostname=hostname)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((key, entry))
    return out


def port_shm(spec, key, entry, hostname):
    """Flux d'un port : gabarit `shm` (sorties) sinon champ `shm_field` de l'élément (entrées)."""
    if spec.get("shm"):
        return fill(spec["shm"], entry, hostname=hostname, key=key, key_flat=key.replace("/", "_"))
    raw = get_path(entry, spec.get("shm_field", "shm")) or ""
    pre = spec.get("shm_prefix", "")
    return raw[len(pre):] if pre and raw.startswith(pre) else raw


def pick(cands, to_slot):
    """Spécification visée par `to_slot` parmi des candidates en mode clé : la clé exacte, sinon un
    entier lu comme POSITION (les appelants qui numérotent — NMOS — n'ont que l'indice)."""
    keyed = [x for x in cands if x.get("key") is not None]
    for x in keyed:
        if str(x.get("slot")) == str(to_slot):
            return x
    try:
        i = int(to_slot)
    except (TypeError, ValueError):
        return None
    return keyed[i] if 0 <= i < len(keyed) else None


def with_field(params, spec, key, value, hostname=""):
    """Copie de `params` où l'élément de clé `key` porte `value` dans `shm_field`. Rend
    (nouveaux_params, erreur) — erreur non vide si la clé n'existe plus."""
    new = copy.deepcopy(params or {})
    for k, entry in expand(spec, new, hostname):
        if k == key:
            set_path(entry, spec.get("shm_field", "shm"), value)
            return new, ""
    return None, "port %s inexistant" % key


def _fresh(vmid):
    from . import plugin_persist
    return plugin_persist._dc(vmid) or {}


def _refusal(type_, errors):
    """Refus de câblage porté par les erreurs du MODÈLE du plugin ({code, path, params…}) : l'interface les
    traduit par plugin.<type>.verr.<code>, comme l'éditeur du plugin."""
    return {"error": "invalid", "plugin": type_, "errors": errors}


def check_wired(vmid, type_, new_params):
    """Erreurs que le document câblé ferait naître, selon le hook `check_wired_params` du plugin (console
    audio : boucle externe). [] sans hook. Un hook qui lève est journalisé et ne bloque pas le câble (le
    conteneur, lui, revalide tout au démarrage et le dit)."""
    import logging
    from . import plugins as _pl
    hook = _pl.get_hook(type_, "check_wired_params")
    if not hook:
        return []
    try:
        from .database import db_get_container
        host = (db_get_container(vmid) or {}).get("hostname") or (new_params or {}).get("hostname") or ""
        return list(hook(new_params, {"vmid": vmid, "type": type_, "hostname": host}) or [])
    except Exception as e:
        logging.getLogger(__name__).warning("check_wired_params %s #%s failed: %s", type_, vmid, e)
        return []


def _store(vmid, dc, params, spec, key, value, host):
    """Écrit le champ dans deploy_config seul (rien n'est appliqué : l'appelant redéploie). Le document n'est
    pas revu par le conteneur : le hook du plugin le contrôle ici (cf. check_wired), refus = rien n'est écrit."""
    from .database import db_update_deploy_config
    new, err = with_field(params, spec, key, value, host)
    if err:
        return False, 400, {"error": err}
    errors = check_wired(vmid, dc["type"], new)
    if errors:
        return False, 422, _refusal(dc["type"], errors)
    db_update_deploy_config(vmid, dc["type"], new)
    return True, 200, {"persisted": True, "applied": False}


def wire(vmid, manifest, spec, key, value):
    """Écrit `value` dans le champ câblé de l'élément `key`. Rend (ok, status, payload).

    Sous `verrou_vmid(op="config")`, sur la config RELUE — jamais l'instantané de l'appelant, qui
    écraserait un réglage concurrent. Si le plugin déclare `control.persist.structure`, le document
    passe par le même chemin qu'une édition de structure à la main (validation à blanc par le
    conteneur, persistance du document canonique, application, retour arrière si refus) ; la base
    de comparaison est le document VIVANT du conteneur (versionné) quand il répond. Sinon, ou
    conteneur hors ligne : la clé est écrite en base et l'appelant redéploie."""
    from . import plugin_persist
    from .vmlocks import verrou_vmid
    with verrou_vmid(vmid, op="config"):
        dc = _fresh(vmid)
        if not dc.get("type"):
            return False, 404, {"error": "container #%s sans déploiement" % vmid}
        params = dict(dc.get("params") or {})
        host = params.get("hostname") or ""
        root = (spec.get("from_list") or "").split(".")[0]
        sp = plugin_persist.spec(manifest)
        if not sp or sp["param"] != root or not sp.get("apply"):
            return _store(vmid, dc, params, spec, key, value, host)
        base = None
        url = plugin_persist._url(vmid, sp["snapshot"])
        if url:
            try:
                r = requests.get(url, timeout=HTTP_TIMEOUT_S)
                if r.status_code == 200 and isinstance(r.json(), dict):
                    base = r.json()
            except Exception:
                base = None
        if base is None:                                  # conteneur muet : on écrit la base seule
            return _store(vmid, dc, params, spec, key, value, host)
        doc, err = with_field({root: base}, spec, key, value, host)
        if err:
            return False, 400, {"error": err}
        res = plugin_persist.structure_apply(
            vmid, manifest, sp["apply"], {"doc": doc[root], "base_version": base.get("version")})
        text, status = res[0], res[1]
        try:
            body = json.loads(text)
        except (TypeError, ValueError):
            body = {"error": str(text)[:300]}
        if status != 200:
            out = {"error": body.get("error") or "refus du conteneur", "detail": body}
            if isinstance(body, dict) and body.get("errors"):     # refus du modèle (boucle externe…) : codes traduits
                out.update(_refusal(dc["type"], body["errors"]))
            return False, status, out
        return True, 200, {"persisted": True, "applied": True, "version": body.get("version")}
