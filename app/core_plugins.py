# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des services infrastructure + versioning/export/import.

Scanne `services/` à la racine du repo : chaque sous-dossier contenant un
`manifest.json` valide est un service. Même principe que app/plugins.py
pour les container plugins.
"""
import datetime
import importlib
import json
import logging
import os
import re
import shutil
import sys

from . import version as _v

log = logging.getLogger(__name__)

SERVICES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services")

_SAFE_ID     = re.compile(r'^[A-Za-z0-9_-]+$')
_REQUIRED    = {"id", "label", "version", "nav_tab", "tab_template", "settings_keys"}

_registry = None  # cache : dict id → {"manifest": ..., "module": ..., "dir": ...}


# ─── Scan & reload ───────────────────────────────────────────────────────────

def scan() -> dict:
    global _registry
    if _registry is not None:
        return _registry
    _registry = {}
    if not os.path.isdir(SERVICES_DIR):
        log.warning(f"core_plugins: dossier services/ introuvable ({SERVICES_DIR})")
        return _registry
    for name in sorted(os.listdir(SERVICES_DIR)):
        svc_dir = os.path.join(SERVICES_DIR, name)
        if not os.path.isdir(svc_dir):
            continue
        manifest_path = os.path.join(svc_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            log.warning(f"core_plugins: {name}/manifest.json illisible : {e}")
            continue
        missing = _REQUIRED - set(manifest.keys())
        if missing:
            log.warning(f"core_plugins: {name}/manifest.json incomplet ({missing}), skipped")
            continue
        try:
            mod = importlib.import_module(f"services.{name}")
        except Exception as e:
            log.warning(f"core_plugins: import services.{name} échoué : {e}, skipped")
            continue
        _registry[manifest["id"]] = {"manifest": manifest, "module": mod, "dir": svc_dir}
    return _registry


def reload():
    """Invalide le cache du registre et re-scanne les manifestes services depuis le disque.

    NE supprime PAS les modules services.* de sys.modules : ils possèdent des blueprints Flask
    enregistrés UNE FOIS au boot (main.py) + de l'état runtime vivant (registre NMOS _receivers,
    tally TSL, arbres Ember+, connexions panneaux Skaarhoj…). Les réimporter dédoublerait
    l'objet-module → l'état peuplé/servi par les routes (ancien module) divergerait de celui lu
    par le code applicatif via `from services import x` (nouveau module vide). C'était la cause
    du bug « page Sources 2110 vide alors que les Câbles montrent les entrées » : IS-04 voyait le
    registre peuplé, `_compute_receivers_detail` un registre vide.

    Les versions rapportées à un pair (raison d'être de ce reload, cf. updater.py) viennent des
    manifest.json relus par scan(), PAS du module Python — un re-scan suffit. Un vrai changement
    de CODE service exige un redémarrage (convention projet)."""
    global _registry
    _registry = None
    scan()


# ─── Helpers registre ────────────────────────────────────────────────────────

def _entry(service_id):
    return next((e for e in scan().values() if e["manifest"]["id"] == service_id), None)


def is_service(service_id) -> bool:
    return _entry(service_id) is not None


def service_actions(availability=None, pid=None):
    """Catalogue des ACTIONS DE SERVICES (chantier 6) : les manifestes de services
    déclarent `actions: [{id, label, params, availability: project|system}]`. Les
    options dynamiques d'un param (panels, presets, sources, colonnes…) sont résolues
    via le hook optionnel `action_options(action_id, key[, pid])` du module — un param
    porte `options_hook: true` pour la déclencher. Le `pid` (contexte projet) est passé
    au hook s'il l'accepte (signature à 3 args) → listes bornées au projet.
    `availability="project"` ne renvoie que les actions utilisables par les macros de PROJET."""
    import inspect
    out = []
    for entry in scan().values():
        m = entry["manifest"]
        acts = m.get("actions") or []
        if not acts:
            continue
        hook = getattr(entry["module"], "action_options", None)
        try:
            hook_nargs = len(inspect.signature(hook).parameters) if callable(hook) else 0
        except (TypeError, ValueError):
            hook_nargs = 2
        resolved = []
        for a in acts:
            avail = a.get("availability") or "system"
            if availability and avail != availability:
                continue
            a = dict(a)
            params = []
            for p in (a.get("params") or []):
                p = dict(p)
                if p.get("options_hook") and callable(hook):
                    try:
                        p["options"] = (hook(a["id"], p["key"], pid) if hook_nargs >= 3
                                        else hook(a["id"], p["key"])) or []
                    except Exception as e:
                        log.debug(f"core_plugins: action_options {m['id']}.{a['id']}.{p['key']}: {e}")
                        p["options"] = []
                params.append(p)
            a["params"] = params
            resolved.append(a)
        if resolved:
            out.append({"service": m["id"], "label": m.get("label") or m["id"],
                        "actions": resolved})
    return out


def run_service_action(service_id, action_id, params, ctx=None):
    """Exécute une action de service (in-process — les services SONT l'orchestrateur).
    Valide contre le manifeste (id + availability selon ctx.project_id) puis délègue
    au hook `run_action(action_id, params, ctx)` du module. Lève sur erreur."""
    entry = _entry(service_id)
    if not entry:
        raise RuntimeError(f"service inconnu : {service_id}")
    act = next((a for a in (entry["manifest"].get("actions") or [])
                if a.get("id") == action_id), None)
    if not act:
        raise RuntimeError(f"action « {action_id} » inconnue pour le service {service_id}")
    ctx = ctx or {}
    if ctx.get("project_id") and (act.get("availability") or "system") != "project":
        raise RuntimeError(f"{service_id}.{action_id} est réservée aux macros système")
    fn = getattr(entry["module"], "run_action", None)
    if not callable(fn):
        raise RuntimeError(f"le service {service_id} n'expose pas run_action")
    return fn(action_id, params or {}, ctx)


def register_all_routes(bp):
    for entry in scan().values():
        fn = getattr(entry["module"], "register_routes", None)
        if callable(fn):
            try:
                fn(bp)
            except Exception as e:
                log.error(f"core_plugins: register_routes({entry['module'].__name__}) : {e}")


def all_settings_defaults() -> dict:
    defaults = {}
    for entry in scan().values():
        for key, spec in entry["manifest"].get("settings_keys", {}).items():
            defaults[key] = spec.get("default")
    return defaults


def settings_schema() -> dict:
    """Schéma ENRICHI de tous les réglages déclarés par les services, pour le RENDU GÉNÉRIQUE de la
    page Réglages (refonte IA). key → {type, default, label, scope, group, help, min, max, step,
    options, service}. Fallbacks : label = clé humanisée, scope='global' (override par-nœud =
    'node'), group = tab_group|service. Source unique « ce qui dépend d'un plugin vit dans le
    plugin » → on étend simplement `settings_keys` du manifeste (rétrocompatible : un spec minimal
    {type, default} marche ; les champs optionnels enrichissent le rendu)."""
    out = {}
    for entry in scan().values():
        m = entry["manifest"]
        svc = m.get("id")
        for key, spec in (m.get("settings_keys") or {}).items():
            s = dict(spec or {})
            s.setdefault("label", key.replace("_", " ").strip().capitalize())
            s.setdefault("scope", "global")
            s.setdefault("group", m.get("tab_group") or svc)
            s["service"] = svc
            out[key] = s
    return out


def manifest_list() -> list:
    reg = scan()
    result = []
    for name in (sorted(os.listdir(SERVICES_DIR)) if os.path.isdir(SERVICES_DIR) else []):
        for entry in reg.values():
            if entry["module"].__name__ == f"services.{name}":
                result.append(entry["manifest"])
                break
    return result


def tab_groups() -> list:
    reg = scan()
    groups, order = {}, []
    entries = [(e["manifest"].get("order", 99), name, e)
               for name, e in ((n, next((x for x in reg.values()
                                         if x["module"].__name__ == f"services.{n}"), None))
                                for n in (sorted(os.listdir(SERVICES_DIR))
                                          if os.path.isdir(SERVICES_DIR) else []))
               if e is not None]
    entries.sort(key=lambda x: (x[0], x[1]))
    for _, name, entry in entries:
        m = entry["manifest"]
        tg = m.get("tab_group")
        if tg:
            if tg not in groups:
                groups[tg] = []
                order.append(("group", tg))
            groups[tg].append(m)
        else:
            order.append(("standalone", m))
    result, seen = [], set()
    for kind, val in order:
        if kind == "group" and val not in seen:
            seen.add(val)
            sub = sorted(groups[val], key=lambda x: x.get("tab_order", 99))
            result.append({"nav_tab": val, "label": _libelle_groupe(val),
                           "sub_tabs": [{"id": s["id"], "label": _libelle(s["id"], s["label"]),
                                          "tab_template": s["tab_template"]} for s in sub]})
        elif kind == "standalone":
            m = val
            result.append({"id": m["id"], "nav_tab": m["nav_tab"],
                           "label": _libelle(m["id"], m["label"]),
                           "tab_template": m["tab_template"], "sub_tabs": None})
    return result


# ─── Libellés d'onglet traduits ──────────────────────────────────────────────
# Par CONVENTION DE CLÉ, comme pour les plugins : le manifeste d'un sous-module reste en
# français et n'a rien à savoir de l'i18n. Sans clé au catalogue, on rend le manifeste.

def _libelle(sid, defaut):
    from . import i18n
    cle = f"service.{sid}.label"
    v = i18n.t(cle)
    return defaut if v == cle else v


def _libelle_groupe(nav_tab):
    from . import i18n
    cle = f"service.group.{nav_tab}"
    v = i18n.t(cle)
    return nav_tab.capitalize() if v == cle else v


# ─── Versioning ──────────────────────────────────────────────────────────────

def _ver_key(v):
    try:
        return (0, tuple(int(x) for x in str(v).split(".")))
    except (ValueError, AttributeError):
        return (1, (str(v),))


def versions(service_id) -> list:
    """Versions disponibles : courante en tête, archivées décroissantes."""
    entry = _entry(service_id)
    if not entry:
        return []
    cur = entry["manifest"].get("version")
    out = [cur]
    vdir = os.path.join(entry["dir"], "versions")
    if os.path.isdir(vdir):
        archived = [d for d in os.listdir(vdir)
                    if os.path.isdir(os.path.join(vdir, d)) and
                    os.path.isfile(os.path.join(vdir, d, "manifest.json"))]
        for v in sorted(archived, key=_ver_key, reverse=True):
            if v not in out:
                out.append(v)
    return out


def _read_meta(path) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def versions_meta(service_id) -> list:
    entry = _entry(service_id)
    if not entry:
        return []
    cur = entry["manifest"].get("version")
    result = []
    for v in versions(service_id):
        if v == cur:
            meta = _read_meta(os.path.join(entry["dir"], "meta.json"))
        else:
            meta = _read_meta(os.path.join(entry["dir"], "versions", v, "meta.json"))
        # Même lecteur que les plugins (app/plugins.parse_meta_changes) : les services titrent
        # eux aussi leurs sections librement (« Corrections (0.3.1) », « Nouveautés (0.1.12) »),
        # et ne lire que les trois clés nues rendait un changelog vide ou périmé.
        from .plugins import parse_meta_changes
        secs, ch_, fx_, kb_ = parse_meta_changes(meta.get("changes"), v)
        result.append({
            "version":      v,
            "current":      v == cur,
            "published_at": meta.get("published_at") or meta.get("date"),
            "imported_at":  meta.get("imported_at"),
            "sections":     secs,
            "changes":      ch_,
            "fixes":        fx_,
            "known_bugs":   kb_,
        })
    return result


# ─── Validation & install ─────────────────────────────────────────────────────

def validate_package(src_dir) -> tuple:
    """Valide un dossier service extrait. Retourne (manifest, None) ou (None, raison)."""
    mp = os.path.join(src_dir, "manifest.json")
    if not os.path.isfile(mp):
        return None, "manifest.json manquant"
    try:
        with open(mp, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return None, f"manifest.json invalide : {e}"
    missing = _REQUIRED - set(manifest.keys())
    if missing:
        return None, f"clés manquantes : {', '.join(sorted(missing))}"
    # ★ EXIGENCE DE VERSION DU CŒUR — même garde que pour les plugins, au même endroit : la
    # fonction de validation, par où passent TOUTES les voies d'installation. Un service peut
    # avoir besoin d'un orchestrateur récent (`services/tsl` importe `app.tally`) ; installé sur
    # un cœur trop ancien, il casse à l'import et ne démarre pas.
    _cmin = _v.core_min_de(manifest)
    if _cmin and not _v.au_moins(_v.VERSION, _cmin):
        return None, ("exige Bobi.Studio >= %s (cette instance est en %s)" % (_cmin, _v.VERSION))
    if _cmin and not _v.comparable(_v.VERSION, _cmin):
        log.warning("paquet : exigence de coeur non comparable (exigee %s, courante %s) — "
                    "controle IGNORE", _cmin, _v.VERSION)
    if not _SAFE_ID.match(str(manifest.get("id", ""))):
        return None, "id invalide (autorisé : lettres, chiffres, _ et -)"
    if not os.path.isfile(os.path.join(src_dir, "__init__.py")):
        return None, "__init__.py manquant"
    return manifest, None


def stamp_imported_at(src_dir):
    p = os.path.join(src_dir, "meta.json")
    data = _read_meta(p)
    data["imported_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _flat_names(d):
    return [n for n in os.listdir(d) if n != "versions" and not n.startswith("__pycache__")]


def _copy_into(src_dir, dst_dir, names):
    os.makedirs(dst_dir, exist_ok=True)
    for name in names:
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        if os.path.isfile(s):
            shutil.copy2(s, d)
        elif os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)


def _clear_flat(d):
    for name in _flat_names(d):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)


def _archive_current(svc_dir, cur_ver):
    dest = os.path.join(svc_dir, "versions", cur_ver)
    os.makedirs(dest, exist_ok=True)
    for name in _flat_names(svc_dir):
        s = os.path.join(svc_dir, name)
        d = os.path.join(dest, name)
        if os.path.isfile(s):
            shutil.copy2(s, d)
        elif os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)


def _merge_versions(src_dir, dst_dir):
    src_vdir = os.path.join(src_dir, "versions")
    if not os.path.isdir(src_vdir):
        return
    dst_vdir = os.path.join(dst_dir, "versions")
    for ver in os.listdir(src_vdir):
        s = os.path.join(src_vdir, ver)
        d = os.path.join(dst_vdir, ver)
        if os.path.isdir(s) and not os.path.isdir(d):
            shutil.copytree(s, d)


def install_package(src_dir, *, activate: bool) -> dict:
    """Installe un paquet dans services/<id>/.
    activate=True → version courante ; activate=False → archivée dans versions/<ver>/."""
    man, err = validate_package(src_dir)
    if err:
        raise ValueError(err)
    svc_id, ver = man["id"], man["version"]
    tdir = os.path.join(SERVICES_DIR, svc_id)
    entry = _entry(svc_id)
    cur = entry["manifest"].get("version") if entry else None
    if activate:
        os.makedirs(tdir, exist_ok=True)
        if cur and cur != ver:
            _archive_current(tdir, cur)
        _clear_flat(tdir)
        _copy_into(src_dir, tdir, _flat_names(src_dir))
        _merge_versions(src_dir, tdir)
    else:
        dest = os.path.join(tdir, "versions", ver)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        _copy_into(src_dir, dest, _flat_names(src_dir))
        _merge_versions(src_dir, tdir)
    reload()
    return {"id": svc_id, "version": ver}


def activate_version(service_id, version) -> dict:
    """Promeut une version archivée en version courante (redémarrage requis)."""
    entry = _entry(service_id)
    if not entry:
        raise ValueError("service inconnu")
    tdir = entry["dir"]
    cur = entry["manifest"].get("version")
    if version == cur:
        return {"id": service_id, "version": version}
    vdir = os.path.join(tdir, "versions", version)
    if not os.path.isdir(vdir):
        raise ValueError(f"version {version} introuvable")
    _archive_current(tdir, cur)
    _clear_flat(tdir)
    _copy_into(vdir, tdir, os.listdir(vdir))
    mp = os.path.join(tdir, "manifest.json")
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("version") != version:
            man["version"] = version
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False, indent=2)
    reload()
    return {"id": service_id, "version": version}


# ─── Export ───────────────────────────────────────────────────────────────────

def export_dir(service_id):
    """Dossier du service à zipper pour l'export complet. None si inconnu."""
    entry = _entry(service_id)
    return entry["dir"] if entry else None


def export_version_dir(service_id, version):
    """Dossier source pour zipper UNE version. Retourne (dir, version) ou (None, None)."""
    entry = _entry(service_id)
    if not entry or version not in versions(service_id):
        return None, None
    cur = entry["manifest"].get("version")
    if version == cur:
        return entry["dir"], version
    return os.path.join(entry["dir"], "versions", version), version
