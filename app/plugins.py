# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Container-type plugin registry.

A *plugin* is a self-contained, versioned container type living in `plugins/<type>/`:

    plugins/<type>/
      plugin.json     # manifest (see REQUIRED_KEYS below)
      script.py       # str.format template -> deployed to the container as /opt/script/main.py
      hooks.py        # optional — lifecycle hooks executed in-process (before_deploy, …)
      control.html    # UI fragment injected into the plugin shell
      control.js      # registers window.MXLPlugins["<type>"] = { mount(el, vmid) }
      control.css     # optional

Hooks (hooks.py) are the ONE exception to the rule "no plugin code in the orchestrator
process": they run in-process but receive only a plain params dict and a minimal context
— no DB handles, no Proxmox token. Each hook is wrapped in try/except; a failing hook
logs a warning and does not abort the deploy.

New-code naming is English by project rule (overrides the French convention in CLAUDE.md).
"""
import importlib.util
from .numerotation import cle_input, cle_input_v, cle_input_a
import json
import logging
import os
import re
import shutil
import time

log = logging.getLogger(__name__)

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")

REQUIRED_KEYS = ("type", "label", "version", "script_template")


def rate_nd(v):
    """Cadence → (num, den) EXACT. Accepte 25, 29.97, "30000/1001". Fractionnaire NTSC = N*1000/1001
    (29.97→30000/1001, 59.94→60000/1001, 23.976→24000/1001). Entier → (n, 1). Miroir du _rate_nd
    inline des plugins de grille — sert au format injecté au câblage (UDC) et au gating #27."""
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/"); return int(a), int(b)
        f = float(v or 25)
    except Exception:
        return 25, 1
    n = round(f)
    if abs(f - n) < 0.01:
        return (n or 25), 1
    nominal = round(f * 1001.0 / 1000.0)
    return nominal * 1000, 1001

# type -> manifest dict (augmented with "_dir" = absolute plugin directory)
REGISTRY = {}

# nom_dossier -> raison (plugins présents sur disque mais NON chargés au dernier scan).
# Rempli par _scan(), exposé via scan_errors() pour l'onglet de gestion des plugins.
SCAN_ERRORS = {}


def _scan():
    """(Re)scan PLUGINS_DIR. Returns {type: manifest}. Tolerant: a broken plugin is
    logged, recorded in SCAN_ERRORS, and skipped — never crashes startup."""
    found = {}
    SCAN_ERRORS.clear()
    if not os.path.isdir(PLUGINS_DIR):
        log.info("plugins: no plugins/ directory (%s)", PLUGINS_DIR)
        return found
    for name in sorted(os.listdir(PLUGINS_DIR)):
        pdir = os.path.join(PLUGINS_DIR, name)
        manifest_path = os.path.join(pdir, "plugin.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            log.error("plugins: bad manifest %s: %s", manifest_path, e)
            SCAN_ERRORS[name] = f"manifeste JSON invalide : {e}"
            continue
        missing = [k for k in REQUIRED_KEYS if k not in manifest]
        if missing:
            log.error("plugins: %s missing keys %s — skipped", manifest_path, missing)
            SCAN_ERRORS[name] = f"clés manquantes dans plugin.json : {', '.join(missing)}"
            continue
        manifest["_dir"] = pdir
        # str.format dry-run guard: catches a literal brace that wasn't doubled before
        # it ever reaches a real deployment.
        tpl_path = os.path.join(pdir, manifest["script_template"])
        try:
            with open(tpl_path, "r", encoding="utf-8") as f:
                tpl = f.read()
            rendu = tpl.format(config="{}", hostname="x", plugin_version="0")
            # ⚠ ET LE SCRIPT RENDU DOIT COMPILER. Le dry-run `.format` ci-dessus ne regarde que
            # les accolades ; pyflakes, lui, analyse le TEMPLATE, où `{config}` se lit comme un
            # littéral d'ensemble parfaitement valide. Ni l'un ni l'autre n'attrape une faute de
            # syntaxe Python — et une seule suffit à mettre le conteneur en boucle de
            # redémarrage, sans que rien ici ne l'ait signalé.
            # Vu le 2026-08-26 : un `global X` placé après une lecture de `X` dans la même
            # fonction (SyntaxError). Le plugin passait le scan, se déployait, et le conteneur
            # relançait le script toutes les deux secondes.
            compile(rendu, tpl_path, "exec")
        except FileNotFoundError:
            log.error("plugins: %s script_template not found (%s) — skipped",
                      manifest["type"], tpl_path)
            SCAN_ERRORS[name] = f"script_template introuvable : {manifest['script_template']}"
            continue
        except SyntaxError as e:
            log.error("plugins: %s script template does not compile (%s) — skipped",
                      manifest["type"], e)
            SCAN_ERRORS[name] = (f"le script rendu ne compile pas : {e.msg} "
                                 f"(ligne {e.lineno})")
            continue
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            # Une accolade non doublée sous forme `{config[x]}` lève TypeError et
            # `{config.attr}` lève AttributeError : les attraper aussi, sinon l'exception
            # remonte jusqu'à REGISTRY = _scan() (niveau module) → ImportError au boot.
            log.error("plugins: %s script template has an unescaped '{' or '}' (%s) — "
                      "double every literal brace. Skipped.", manifest["type"], e)
            SCAN_ERRORS[name] = f"accolade littérale non doublée dans {manifest['script_template']} ({e})"
            continue
        manifest["_hooks"] = _load_hooks(pdir, manifest["type"])
        found[manifest["type"]] = manifest
        log.info("plugins: loaded %s v%s", manifest["type"], manifest["version"])
    return found


def _load_hooks(plugin_dir, type_):
    """Charge hooks.py depuis le dossier du plugin. Retourne le module, ou None si absent/invalide.
    Les erreurs sont loguées mais n'empêchent pas le chargement du plugin."""
    hooks_path = os.path.join(plugin_dir, "hooks.py")
    if not os.path.isfile(hooks_path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"mxl_plugin_{type_}_hooks", hooks_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        log.info("plugins: hooks loaded for %s", type_)
        return mod
    except Exception as e:
        log.warning("plugins: %s hooks.py ignoré (%s)", type_, e)
        return None


def get_hook(type_, hook_name):
    """Retourne la fonction hook_name du plugin type_, ou None si non déclarée."""
    m = REGISTRY.get(type_)
    if not m:
        return None
    hooks_mod = m.get("_hooks")
    if not hooks_mod:
        return None
    return getattr(hooks_mod, hook_name, None)


def scan_errors():
    """{nom_dossier: raison} des plugins présents sur disque mais non chargés au dernier scan."""
    return dict(SCAN_ERRORS)


def reload():
    global REGISTRY
    REGISTRY = _scan()
    _FINGERPRINT.clear()
    _FINGERPRINT.update(_manifests_fingerprint())
    return REGISTRY


# ── Fraîcheur du registre (bug 2026-07-14) ───────────────────────────────────────────────
# REGISTRY était scanné UNE FOIS au boot. Après un bump de plugin SANS redémarrage :
#   • render_script() lit script.py sur le DISQUE      → le conteneur reçoit le code NEUF ;
#   • resolved_version() lit le REGISTRE EN MÉMOIRE    → deploy_config est estampillé à
#     l'ANCIENNE version.
# Résultat : des conteneurs qui exécutent le bon code avec une mauvaise étiquette — et comme la
# garde d'application à chaud compare cette étiquette à la version du manifeste (deploy.py), elle
# échoue TOUJOURS : chaque réglage d'un mur devenait un REDÉPLOIEMENT COMPLET (blip) au lieu d'un
# push à chaud. Le registre se rafraîchit donc quand un manifeste change sur le disque.
_FINGERPRINT = {}          # {chemin plugin.json: mtime} au dernier scan
_FP_LAST_CHECK = [0.0]     # anti-martèlement : au plus un stat de la liste par seconde
_FP_MIN_INTERVAL_S = 1.0


def _manifests_fingerprint():
    """{chemin plugin.json: mtime} — assez pour détecter un bump, un ajout ou un retrait de plugin.
    Le script.py n'y est PAS : il est relu du disque à chaque rendu, il ne peut pas dériver."""
    fp = {}
    if not os.path.isdir(PLUGINS_DIR):
        return fp
    for name in os.listdir(PLUGINS_DIR):
        mp = os.path.join(PLUGINS_DIR, name, "plugin.json")
        try:
            fp[mp] = os.path.getmtime(mp)
        except OSError:
            continue
    return fp


def rescan_if_changed():
    """Re-scanne le registre si un manifeste a changé/apparu/disparu depuis le dernier scan.
    Appelée sur le chemin de DÉPLOIEMENT (render_script/resolved_version) — jamais dans une boucle
    chaude. Throttlée à 1 Hz : ~17 stat() au pire, coût négligeable devant un déploiement."""
    now = time.time()
    if now - _FP_LAST_CHECK[0] < _FP_MIN_INTERVAL_S:
        return False
    _FP_LAST_CHECK[0] = now
    fp = _manifests_fingerprint()
    if fp == _FINGERPRINT:
        return False
    avant = {k: (REGISTRY.get(k) or {}).get("version") for k in REGISTRY}
    reload()
    apres = {k: (REGISTRY.get(k) or {}).get("version") for k in REGISTRY}
    bumps = [f"{k} {avant.get(k)}→{v}" for k, v in apres.items() if avant.get(k) != v]
    log.info("plugins: manifeste(s) modifié(s) sur disque → registre rescanné%s",
             (" (" + ", ".join(bumps) + ")") if bumps else "")
    return True


def _disabled():
    """Set des types de plugins désactivés (politique orchestrateur, stockée en settings
    JSON `plugins_disabled` — pas dans les fichiers du plugin). Un plugin désactivé reste
    installé et déployable (containers existants OK) mais n'apparaît plus dans la palette/nav."""
    try:
        from . import settings
        raw = settings.get("plugins_disabled")
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def is_disabled(type_):
    return type_ in _disabled()


def set_disabled(type_, flag):
    from . import settings
    cur = _disabled()
    cur.add(type_) if flag else cur.discard(type_)
    settings.set("plugins_disabled", json.dumps(sorted(cur)))
    return sorted(cur)


def delete_plugin(type_):
    """Supprime le dossier du plugin sur disque + nettoie son état désactivé + reload.
    Le garde-fou « containers utilisateurs » est appliqué en amont (route)."""
    m = REGISTRY.get(type_)
    if not m:
        raise ValueError("type inconnu")
    shutil.rmtree(m["_dir"])
    set_disabled(type_, False)
    reload()
    return type_


def get(type_):
    return REGISTRY.get(type_)


def all():
    return list(REGISTRY.values())


def is_plugin(type_):
    return type_ in REGISTRY


def needs_dpdk(type_):
    """Vrai si le type est un plugin matériel MTL/DPDK (manifeste `needs_dpdk`). Sert de
    prédicat de routage `is_mtl_type` (docker_compute : chemin docker_driver, --network host,
    PF en AF-XDP). Le moteur 2110_io tourne sur la PF directement, sans VF SR-IOV."""
    m = REGISTRY.get(type_) or {}
    return bool(m.get("needs_dpdk"))


def runtime(type_):
    """Runtime d'exécution du type : 'docker' (défaut) — un conteneur se crée en un coup sur un
    nœud Docker (create+deploy atomiques). Générique : indépendant du nom de type.

    ★ Le défaut était 'lxc', héritage du backend Proxmox RETIRÉ. Il ne décrivait plus rien : le
    seul plugin qui omettait la clé (`tone_gen`) se retrouvait classé non-Docker, donc privé de
    nœud obligatoire, de choix de format et du panneau de charge dans l'assistant de création —
    pour finir sur le 400 « Backend LXC retiré » de `routes/__init__.py`. Un défaut doit désigner
    le cas COURANT : oublier la clé donne maintenant le comportement normal, pas une branche morte."""
    m = REGISTRY.get(type_) or {}
    return (m.get("runtime") or "docker").lower()


def libelle_type(type_, defaut=None):
    """Libellé AFFICHABLE d'un type, traduit dans la langue courante.

    Les manifestes de plugin portent un `label` FRANÇAIS (« Délai », « Mélangeur », « Images
    fixes »). Il était rendu tel quel partout — onglets de rubrique, filtres de la page
    Conteneurs, palette de déploiement, badges de carte, nav — donc une interface en anglais
    gardait ses types en français (signalé en recette le 2026-08-21).

    La traduction se fait ICI, par CONVENTION DE CLÉ (`type.<id>.label`), et NON dans les
    manifestes : `plugins/` et `services/` sont des sous-modules git, y ajouter une clé par
    plugin obligerait à un commit par sous-module pour un texte qui n'est affiché que par
    l'orchestrateur. Clé absente du catalogue → on rend le `label` du manifeste (donc un
    plugin tiers non traduit reste lisible, jamais une clé brute à l'écran).
    """
    m = REGISTRY.get(type_) or {}
    return _traduit(f"type.{type_}.label", defaut if defaut is not None else m.get("label", type_))


def _traduit(cle, defaut):
    """`i18n.t` avec repli sur `defaut` quand la clé n'existe dans AUCUN catalogue (t() rend
    alors la clé elle-même). Jamais d'exception : l'i18n ne doit pas pouvoir casser le registre."""
    try:
        from .i18n import t
        v = t(cle)
        return defaut if v == cle else v
    except Exception:
        return defaut


def sections():
    """Group plugins by nav.section, plugins triés ALPHABÉTIQUEMENT par label (cf.
    _section_tab_sort_key). Used by the layout context processor to render nav entries
    and by the /medias shell to list tabs.

    Returns {section_id: {label, route, order, plugins: [manifest, ...]}}.
    """
    out = {}
    dis = _disabled()
    for m in REGISTRY.values():
        if m.get("type") in dis:        # désactivé → hors palette/nav
            continue
        nav = m.get("nav") or {}
        sec = nav.get("section")
        if not sec:
            continue
        # Route de la RUBRIQUE = toujours le shell unifié /{sec} (page à onglets). On n'hérite
        # PAS du nav.route d'un plugin : celui-ci est un favori legacy par-type (ex.
        # /traitements/correcteurs → redirige en /traitements#color_corrector) et, selon l'ordre
        # de scan, il forçait le lien de nav sur un onglet précis via un #hash (cf. Correcteur
        # toujours sélectionné). Les routes par-plugin restent dispo dans chaque manifeste (nav.route).
        entry = out.setdefault(sec, {
            # Libellé de RUBRIQUE (« Traitements », « Médias ») : même convention de clé que les
            # types, côté cœur — cf. libelle_type().
            "label": _traduit(f"section.nav.{sec}", nav.get("label", sec.capitalize())),
            "route": f"/{sec}",
            "order": nav.get("order", 100),
            "plugins": [],
        })
        # Copie SUPERFICIELLE : REGISTRY est partagé (scanné une fois) et sert aussi au rendu de
        # script ; y écrire un label traduit contaminerait tout le process, langue comprise.
        entry["plugins"].append(dict(m, label=libelle_type(m.get("type"))))
        # section-level order = smallest plugin order
        entry["order"] = min(entry["order"], nav.get("order", 100))
    for entry in out.values():
        entry["plugins"].sort(key=_section_tab_sort_key)
        entry["groupes"] = _groupes_de(entry["plugins"])
    return out


def _groupes_de(plugins_tries):
    """Sous-groupes d'une rubrique, dérivés de `nav.group` des manifestes.

    Une rubrique peut mériter une subdivision sans mériter d'être coupée en deux : dans Sources,
    « ce qui arrive d'ailleurs » et « ce qu'on fabrique ici » ne se cherchent pas dans le même état
    d'esprit, mais ce sont bien toutes des sources. Le groupe est donc PRÉSENTATIONNEL — il ne crée
    ni route, ni rubrique, ni sémantique de câblage.

    Retourne [] quand aucun plugin de la rubrique ne déclare de groupe : une subdivision à un seul
    tas n'est pas une subdivision, et les rubriques non concernées ne doivent rien changer à leur
    rendu. Sinon [{id, label, plugins:[…]}] dans l'ordre de première apparition, les plugins SANS
    groupe formant un dernier tas anonyme plutôt que de disparaître."""
    if not any((m.get("nav") or {}).get("group") for m in plugins_tries):
        return []
    ordre, par_id = [], {}
    for m in plugins_tries:
        gid = (m.get("nav") or {}).get("group") or ""
        if gid not in par_id:
            ordre.append(gid)
            par_id[gid] = {"id": gid, "plugins": [],
                           "label": _traduit(f"section.group.{gid}", gid.capitalize()) if gid else ""}
        par_id[gid]["plugins"].append(m)
    return [par_id[g] for g in ordre]


def _section_tab_sort_key(m):
    """Tri des onglets d'une rubrique : ALPHABÉTIQUE par label, insensible à la casse et aux
    accents (« Délai »/« Mélangeur » bien placés ; les é/è ne partent pas après z)."""
    import unicodedata
    lbl = m.get("label") or m.get("type") or ""
    return unicodedata.normalize("NFKD", lbl).encode("ascii", "ignore").decode().lower()


def monitoring_panels():
    """Plugins déclarant un panneau Monitoring (manifest.monitoring). Chaque plugin peut
    contribuer un onglet à la page Monitoring, avec son propre fragment UI (servi via
    /api/plugins/<type>/ui/monitoring_html + monitoring_js, comme control.html/js). Aucun
    code plugin n'est exécuté côté orchestrateur : seul le fragment HTML/JS est servi tel quel.

    Retourne [{type, label, order, when}] trié par order. `when` ∈ {"instances","always"}
    (défaut "instances" → l'onglet n'apparaît que s'il existe ≥1 container de ce type)."""
    out = []
    dis = _disabled()
    for m in REGISTRY.values():
        t = m.get("type")
        if t in dis:
            continue
        mon = m.get("monitoring")
        if not mon:
            continue
        when = str(mon.get("when") or "instances").lower()
        out.append({
            "type": t,
            "label": _traduit(f"type.{t}.monitoring", mon.get("label") or libelle_type(t)),
            "order": mon.get("order", 100),
            "when": when if when in ("instances", "always") else "instances",
        })
    out.sort(key=lambda p: (p["order"], p["type"]))
    return out


def versions(type_):
    """Versions disponibles d'un plugin, de la plus récente à la plus ancienne :
    la version COURANTE (manifest.version, dossier plat) + les versions ARCHIVÉES sous
    `plugins/<type>/versions/<ver>/script.py`. Dédupliqué, courante en tête."""
    m = REGISTRY.get(type_)
    if not m:
        return []
    cur = m.get("version")
    out = [cur]
    vdir = os.path.join(m["_dir"], "versions")
    if os.path.isdir(vdir):
        archived = [d for d in os.listdir(vdir)
                    if os.path.isfile(os.path.join(vdir, d, "script.py"))]
        # tri décroissant « façon version » (numérique par segment, fallback lexical)
        def _key(v):
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (v,)
        for v in sorted(archived, key=_key, reverse=True):
            if v not in out:
                out.append(v)
    return out


# Métadonnées par version (lecture seule côté UI) : changelog + dates.
# Stockées dans un meta.json optionnel à côté du script.py de chaque version
# (plat = courante, sinon versions/<ver>/meta.json). Aucun plugin existant n'en a
# aujourd'hui → lecture tolérante avec défauts vides (rétrocompat).
_META_DEFAULT = {"published_at": "", "imported_at": "",
                 "sections": [], "changes": [], "fixes": [], "known_bugs": []}
_META_LISTS = ("changes", "fixes", "known_bugs")


def _meta_path(type_, version=None):
    """Chemin du meta.json pour (type_, version) : dossier plat si version courante/None,
    sinon versions/<ver>/meta.json. None si type inconnu."""
    m = REGISTRY.get(type_)
    if not m:
        return None
    if not version or version == m.get("version"):
        return os.path.join(m["_dir"], "meta.json")
    return os.path.join(m["_dir"], "versions", version, "meta.json")


def _coerce_meta_list(v):
    """Normalise une valeur changelog → liste de strings (string simple → [string])."""
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


# Un titre de section de changelog porte souvent sa version en fin : « … (0.66.0) ».
_META_VER_RE = re.compile(r"\((\d+(?:\.\d+)+)\)\s*$")
# Classement par PRÉFIXE du titre. Tout le reste est une nouveauté : le parc titre ses
# sections librement (« Libellé de la source sous chaque Rx … »), et une section non
# reconnue doit s'afficher, pas disparaître.
_META_KIND_PREFIX = (
    ("fixes",      ("correction", "fix ", "fix:", "fix(")),
    ("known_bugs", ("bug connu", "bugs connus", "known bug", "limitation")),
)


def _meta_kind(title):
    t = str(title or "").strip().lower()
    for kind, prefixes in _META_KIND_PREFIX:
        if any(t.startswith(p) for p in prefixes):
            return kind
    return "changes"


def parse_meta_changes(raw_changes, version=None):
    """Changelog d'un meta.json → (sections, changes, fixes, known_bugs).

    `sections` = [{title, version, kind, items}] dans l'ordre du fichier — c'est CE format que
    l'UI rend, titres compris. Trois formats coexistent dans le parc et doivent tous marcher :
      • sections à TITRE LIBRE portant la version — « Libellé de la source … (0.66.0) » :
        le format réellement écrit aujourd'hui par tous les plugins ;
      • sections legacy versionnées — « Nouveautés (0.1.12) », « Corrections (0.3.1) » ;
      • sections legacy nues — « Nouveautés », « Corrections », « Bugs connus ».
    Le lecteur historique ne connaissait QUE le troisième : partout ailleurs il rendait une liste
    vide, ou pire, la vieille section « Nouveautés » restée au fond du fichier — donc un changelog
    faux attribué à la version courante (constaté sur 2110_io et multiview).

    `version` (facultatif) : la fiche d'une version ne doit montrer QUE cette version. Dès qu'au
    moins une section porte une version, on filtre dessus — les sections sans version sont alors de
    l'historique ancien, et les agréger sous la version courante laisserait croire qu'elle contient
    tout (mesuré : 70 sections sous « v0.66.0 » pour 2110_io). Si AUCUNE section ne porte de
    version, ou si aucune ne correspond, on renvoie tout : mieux vaut l'historique complet qu'un
    changelog vide qui ferait croire qu'il n'y a rien.
    """
    sections = []
    if isinstance(raw_changes, dict):
        for title, items in raw_changes.items():
            m = _META_VER_RE.search(str(title))
            sections.append({"title": str(title), "version": m.group(1) if m else None,
                             "kind": _meta_kind(title), "items": _coerce_meta_list(items)})
    else:                                   # format plat (legacy) : une simple liste
        items = _coerce_meta_list(raw_changes)
        if items:
            sections.append({"title": "", "version": None, "kind": "changes", "items": items})
    if version and any(s["version"] == version for s in sections):
        sections = [s for s in sections if s["version"] == version]
    out = {"changes": [], "fixes": [], "known_bugs": []}
    for s in sections:
        out[s["kind"]].extend(s["items"])
    return sections, out["changes"], out["fixes"], out["known_bugs"]


def read_version_meta(type_, version=None):
    """Métadonnées d'une version : dict complet (défauts vides si absent/illisible).
    Supporte deux formats de meta.json :
      • format plugins (réel) : {"date", "changes": {"Nouveautés":[…], "Corrections":[…],
        "Bugs connus":[…]}}
      • format plat (legacy)  : {"published_at", "changes":[…], "fixes":[…], "known_bugs":[…]}
    Lecture JSON seule — aucun code plugin exécuté."""
    out = dict(_META_DEFAULT)
    out["sections"] = []
    out["changes"], out["fixes"], out["known_bugs"] = [], [], []
    p = _meta_path(type_, version)
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
        except (ValueError, OSError):
            raw = None
        if isinstance(raw, dict):
            out["published_at"] = raw.get("published_at") or raw.get("date") or ""
            out["imported_at"]  = raw.get("imported_at", "")
            ver = version or (REGISTRY.get(type_) or {}).get("version")
            secs, ch_, fx_, kb_ = parse_meta_changes(raw.get("changes"), ver)
            out["sections"], out["changes"], out["fixes"], out["known_bugs"] = secs, ch_, fx_, kb_
            # Format plat (legacy) : fixes/known_bugs vivent à la racine du meta.json.
            if not isinstance(raw.get("changes"), dict):
                out["fixes"]      = _coerce_meta_list(raw.get("fixes")) or out["fixes"]
                out["known_bugs"] = _coerce_meta_list(raw.get("known_bugs")) or out["known_bugs"]
    return out


def versions_meta(type_):
    """[{version, current, published_at, imported_at, changes, fixes, known_bugs}, …]
    dans l'ordre de versions() (courante en tête)."""
    cur = (REGISTRY.get(type_) or {}).get("version")
    return [{"version": v, "current": v == cur, **read_version_meta(type_, v)}
            for v in versions(type_)]


def _script_path(type_, version=None):
    """Chemin du script.py à rendre pour (type_, version). version absente/courante →
    dossier plat ; version archivée connue → versions/<ver>/script.py ; sinon None."""
    m = REGISTRY.get(type_)
    if not m:
        return None
    if not version or version == m.get("version"):
        return os.path.join(m["_dir"], m["script_template"])
    cand = os.path.join(m["_dir"], "versions", version, "script.py")
    return cand if os.path.isfile(cand) else None


def resolved_version(type_, version=None):
    """Version réellement déployable : la demandée si disponible, sinon la courante.
    Rafraîchit le registre si un manifeste a bougé : sinon on estampille une version PÉRIMÉE
    dans deploy_config alors que le script rendu, lui, vient du disque (cf. rescan_if_changed)."""
    rescan_if_changed()
    m = REGISTRY.get(type_)
    if not m:
        return None
    return version if (version and version in versions(type_)) else m.get("version")


def render_script(type_, params, hostname, version=None):
    """Render a plugin's deployed script via str.format. Single structured placeholder
    `{config}` (repr of params) + `{hostname}` + `{plugin_version}`. `version` choisit la
    version archivée à rendre (défaut = courante) ; `plugin_version` injecté = version rendue.
    Rafraîchit le registre si un manifeste a bougé (cf. rescan_if_changed) : le script vient du
    DISQUE, la version venait de la MÉMOIRE — les deux doivent parler du même bump."""
    rescan_if_changed()
    m = REGISTRY.get(type_)
    if not m:
        return None
    ver = resolved_version(type_, version)
    path = _script_path(type_, ver) or os.path.join(m["_dir"], m["script_template"])
    with open(path, "r", encoding="utf-8") as f:
        tpl = f.read()
    cfg = dict(params or {})
    # Filet : un multiview sans format de sortie explicite hérite du défaut SYSTÈME (jamais un
    # littéral) — couvre les conteneurs legacy/edge non semés à la création. L'explicite prime.
    if type_ == "multiview":
        from .scripts import multiview_output_format_defaults
        for k, v in multiview_output_format_defaults().items():
            if not cfg.get(k):
                cfg[k] = v
    # Même filet pour TOUT TYPE QUI DÉCLARE UN CANVAS : format sans valeur explicite → format
    # par défaut SYSTÈME (jamais le littéral 720p du plugin.json — cf. effective_deploy_defaults).
    #
    # ⚠ C'ÉTAIT UNE LISTE EN DUR ("mixer", "avsync"), et elle a récidivé : la sonde de latence,
    # ajoutée le 2026-08-11, a publié son panneau en 1280×720p25 8 bits sur une maison en
    # 1920×1080p50 10 bits — parce que personne n'a pensé à l'inscrire ici. C'est exactement la
    # panne que `_FORMAT_NOT_FROM_SETTINGS` documente deux fonctions plus bas, reproduite par le
    # filet censé l'empêcher. La règle est donc la MÊME des deux côtés, et déclarative : un type
    # qui déclare width+height suit le format système, sauf exclusion explicite et justifiée.
    _dd = (REGISTRY.get(type_) or {}).get("deploy_defaults") or {}
    if ("width" in _dd and "height" in _dd) and type_ not in _FORMAT_NOT_FROM_SETTINGS:
        from .scripts import get_default_video_format
        _f = get_default_video_format()
        for k, v in (("width", int(_f["width"])), ("height", int(_f["height"])),
                     ("fps", _f["fps"]), ("scan", _f.get("scan") or "p"),
                     ("chroma", _f.get("chroma") or "422"),
                     ("bit_depth", int(_f.get("bit_depth") or 10))):
            if k in _dd and not cfg.get(k):
                cfg[k] = v
    return tpl.format(config=repr(cfg),
                      hostname=hostname,
                      plugin_version=ver)


def ui_asset_path(type_, key, version=None):
    """Absolute path to a UI asset declared under manifest.ui[key], sanitized to stay
    inside the plugin directory. If version is given, looks in versions/<version>/ first
    (fallback to flat dir if the archive is script-only). Returns None if absent/escaping."""
    m = REGISTRY.get(type_)
    if not m:
        return None
    rel = (m.get("ui") or {}).get(key)
    if not rel:
        return None
    base = os.path.realpath(m["_dir"])
    if version:
        vpath = os.path.realpath(os.path.join(base, "versions", version, rel))
        if vpath.startswith(base + os.sep) and os.path.isfile(vpath):
            return vpath
    path = os.path.realpath(os.path.join(base, rel))
    if not (path == base or path.startswith(base + os.sep)):
        return None
    return path if os.path.isfile(path) else None


def wants_media_volume(type_):
    """True si le plugin déclare manifest.media_volume → le container reçoit le bind
    mount du volume média partagé (hôte → /mnt/media) à la création."""
    return bool((REGISTRY.get(type_) or {}).get("media_volume"))


def wants_state_volume(type_):
    """True si le plugin déclare manifest.state_volume → le container reçoit un dossier
    d'état PERSISTANT sur le nœud (hôte → /var/lib/bobi). Le rootfs d'un container est
    recréé à chaque déploiement et au boot du nœud : tout ce qui doit survivre (journal
    d'exploitation, pièce justificative) s'écrit là, pas dans /opt/script."""
    return bool((REGISTRY.get(type_) or {}).get("state_volume"))


def image_variant(type_):
    """Variante d'image Docker du plugin (chemin compute) : 'compute' (défaut, bobi-compute)
    ou 'media' (bobi-media : GStreamer+ffmpeg pour player/recorder/transcoder). Le nœud porte
    une image par variante (compute_image / media_image)."""
    v = str((REGISTRY.get(type_) or {}).get("image") or "compute").lower()
    return v if v in ("compute", "media") else "compute"


def image_kind(type_):
    """Nature de l'image Docker RÉELLEMENT requise par un type au déploiement :
      · 'mtl'     → moteur ST 2110 (node.image / bobi-mtl, chemin docker_driver) ;
      · 'webrtc'  → passerelle MediaMTX (setting webrtc_image) ;
      · 'compute' | 'media' → variante compute/média générique (docker_compute).
    Sert à la palette de création pour ne griser un nœud QUE si l'image du type choisi y manque
    (≠ `image_ok` de /api/nodes, qui ne teste QUE bobi-mtl et grisait donc à tort les nœuds
    compute/média dépourvus du moteur). Le GPU est une VARIANTE optionnelle de 'compute' (repli
    transparent numpy si absent) → jamais un kind à part, jamais bloquant."""
    if not type_:
        return "compute"
    if needs_dpdk(type_):
        return "mtl"
    if type_ == "webrtc_gateway":
        return "webrtc"
    return image_variant(type_)


def wants_gpu(type_):
    """True si le plugin préfère le GPU (manifest.resources.gpu truthy). Sur un nœud GPU-capable
    (node.gpu_capable + compute_gpu_image), le déploiement choisit l'image GPU + injecte --gpus ;
    sinon repli transparent sur l'image compute CPU (le script.py auto-détecte cupy → numpy)."""
    return bool(((REGISTRY.get(type_) or {}).get("resources") or {}).get("gpu"))


def gpu_optional(type_):
    """True si le plugin PEUT tirer parti d'un GPU sans l'exiger (`manifest.resources.gpu_optional`).

    Distinct de `wants_gpu` (`resources.gpu`), qui déclare un besoin de TYPE : le GPU est alors
    alloué à toute instance. Un `streamer` n'a besoin d'une carte que s'il encode en NVENC — et
    seule l'INSTANCE le sait, par ses params. Déclarer `gpu` sur le type ferait réserver une carte
    à chaque streamer en x264 ; avec quatre GPU exploitables sur le parc, quelques instances
    suffiraient à tout bloquer. D'où cette seconde notion : le type se dit CAPABLE, l'allocation
    se décide sur les params (cf. `docker_compute._veut_gpu_instance`)."""
    return bool(((REGISTRY.get(type_) or {}).get("resources") or {}).get("gpu_optional"))


def gpu_instance_rule(type_):
    """Règle DÉCLARÉE qui dit, pour un type seulement `gpu_optional`, quelle instance mérite une
    carte : `resources.gpu_instance = {"param": "video.encoder", "values": ["nvenc", "auto"]}`.

    Le `param` est un chemin pointé dans `deploy_config.params`. Sans règle déclarée, l'appelant
    garde l'heuristique historique du `streamer` — mais tout NOUVEAU plugin GPU-capable doit
    déclarer la sienne ici plutôt que d'ajouter un `if type == …` dans l'orchestrateur : c'est
    exactement le genre de branche qui se duplique et se désynchronise du manifeste."""
    r = ((REGISTRY.get(type_) or {}).get("resources") or {}).get("gpu_instance")
    if not isinstance(r, dict) or not r.get("param"):
        return None
    vals = r.get("values")
    return {"param": str(r["param"]),
            "values": [str(v).lower() for v in (vals if isinstance(vals, list) else [vals])]}


def gpu_capable(type_):
    """True si le plugin sait exploiter un GPU, que ce soit un besoin (`gpu`) ou une option
    (`gpu_optional`). C'est le prédicat du PLACEMENT : un type GPU-capable préfère un nœud à
    carte, un type qui ne l'est pas doit au contraire les laisser libres."""
    return wants_gpu(type_) or gpu_optional(type_)


def wants_brand(type_):
    """True si le plugin déclare manifest.brand → deploy injecte la marque (textes +
    logo base64) dans ses params au déploiement (cf. deploy._resolve_brand_settings)."""
    return bool((REGISTRY.get(type_) or {}).get("brand"))


def options_dynamiques(source):
    """Options d'un champ `select` qui déclare `options_from` — la liste vit AILLEURS.

    ★ POURQUOI CE MÉCANISME. Un manifeste est un fichier statique ; certaines listes
    ne le sont pas. Le format vidéo en est le cas type : le produit tient déjà une
    liste NOMMÉE dans Réglages → Vidéo, avec scan, chroma, profondeur et
    colorimétrie. Sans ce crochet, un plugin qui veut y puiser n'a que deux
    mauvaises options — figer une copie dans son manifeste (qui dérive au premier
    ajout) ou demander des pixels à la main (qui ne dit rien du scan ni de la
    profondeur, et invite à saisir un format qui n'existe nulle part ailleurs).

    Générique à dessein : le plugin déclare `"options_from": "<source>"`, il ne
    connaît ni les Réglages ni la base. Une source inconnue rend une liste vide,
    donc un select vide — jamais une exception sur le chemin d'affichage d'une
    palette.
    """
    if source == "tally_levels":
        # Les niveaux de tally sont des ENTITÉS NOMMÉES du site, allouées dans Réglages →
        # Labels & Tally. Un plugin ne peut ni les deviner ni en figer la liste : elle change
        # dès qu'une production est créée. Le libellé montre le nom écrit par l'exploitant —
        # « niveau 7 » ne dit rien, « 7 — Antenne » se choisit sans aller vérifier ailleurs.
        # ★ LA VALEUR EST L'UUID, LE LIBELLÉ PORTE LE NUMÉRO. Le numéro n'est qu'un rang
        # d'affichage : réordonner les niveaux le réécrit, et une configuration qui l'aurait
        # mémorisé pointerait ensuite un autre niveau — silencieusement, sur une fonction
        # d'antenne. L'exploitant lit « 3 — Plateau », le manifeste garde l'identité.
        try:
            from .database import db_get_tally_levels
            return [{"value": n["uuid"],
                     "label": "%d — %s" % (n.get("num") or 0, n.get("nom") or "?")}
                    for n in (db_get_tally_levels() or [])]
        except Exception:
            return []
    if source != "video_formats":
        return []
    try:
        from . import settings as _st
        lignes = [l for l in ((_st.all() or {}).get("video_formats") or "").split("\n") if l.strip()]
    except Exception:
        return []
    # ⚠ CE LIBELLÉ EST DU TEXTE D'INTERFACE, pas une donnée. Les NOMS de préréglages
    # viennent des réglages du site et ne se traduisent pas — celui-ci, si. Écrit en
    # dur, il restait français dans une interface en anglais (constaté en recette).
    out = [{"value": "", "label": _traduit("plugins.opt.format_defaut", "— défaut du site —")}]
    for l in lignes:
        parts = [x.strip() for x in l.split(";")]
        nom = parts[0]
        if not nom:
            continue
        # Le libellé montre ce que le nom cache : « HD 1080i50 » ne dit ni la
        # chroma ni la profondeur, et c'est justement ce qu'on ne veut pas
        # laisser deviner.
        détail = ""
        if len(parts) >= 4:
            détail = " — %sx%s @ %s" % (parts[1], parts[2], parts[3])
            if len(parts) >= 5 and parts[4]:
                détail += parts[4]
            if len(parts) >= 7:
                détail += " %s bits" % parts[6]
        out.append({"value": nom, "label": nom + détail})
    return out


def config_schemas():
    """{type: config_schema} pour tous les plugins déclarant un `config_schema`
    (Tier 1). Exposé au client pour rendre/collecter les champs dans la palette.

    Les textes VISIBLES du schéma (label du champ, aide, libellés d'options) sont traduits ici,
    par la même convention de clé que les libellés de type (cf. libelle_type) :
        type.<id>.cfg.<key>.label / .help / .opt.<valeur>
    Ils vivent dans les manifestes en français ; la palette de création les affichait donc en
    français quelle que soit la langue (signalé en recette le 2026-08-21). Clé absente → texte du
    manifeste : un plugin tiers non traduit reste lisible.

    On rend des COPIES : REGISTRY est partagé, y écrire un texte traduit fixerait la langue du
    premier visiteur pour tout le process."""
    out = {}
    for t, m in REGISTRY.items():
        cs = m.get("config_schema")
        if not cs:
            continue
        champs = []
        for f in cs:
            g = _champ_traduit(t, f)
            # Options dynamiques : résolues À LA LECTURE, jamais écrites dans le
            # registre (qui est partagé — y figer une liste la périmerait pour
            # tout le process au premier changement de réglage).
            if isinstance(g, dict) and g.get("options_from") and not g.get("options"):
                g["options"] = options_dynamiques(g["options_from"])
            champs.append(g)
        out[t] = champs
    return out


def _champ_traduit(type_, champ):
    """Copie d'un champ de config_schema dont label/help/options sont passés au catalogue."""
    if not isinstance(champ, dict):
        return champ
    cle = champ.get("key") or ""
    f = dict(champ)
    # Un manifeste peut DÉCLARER lui-même sa clé (`label_i18n`/`help_i18n`) : cette convention
    # existait avant la nôtre, ses clés sont au catalogue du cœur — mais rien ne les lisait, donc
    # le champ restait français alors que sa traduction était écrite (constaté 2026-08-25).
    # La déclaration explicite gagne ; sinon on retombe sur la convention `type.<id>.cfg.<clé>.*`.
    # ⚠ Un champ peut n'avoir QUE `label_i18n`, sans `label` : le libellé est alors absent du
    # manifeste et l'UI retombe sur la CLÉ TECHNIQUE (« light_angle » à l'écran). Il faut donc
    # résoudre la déclaration même quand `label` est vide — c'était le cas des quatre champs
    # d'éclairage et de transition de `split` (constaté 2026-08-25).
    if f.get("label") or f.get("label_i18n"):
        f["label"] = _traduit(f.get("label_i18n") or f"type.{type_}.cfg.{cle}.label",
                              f.get("label") or cle)
    if f.get("help") or f.get("help_i18n"):
        f["help"] = _traduit(f.get("help_i18n") or f"type.{type_}.cfg.{cle}.help",
                             f.get("help") or "")
    if f.get("placeholder"):
        f["placeholder"] = _traduit(f"type.{type_}.cfg.{cle}.ph", f["placeholder"])
    opts = f.get("options")
    if isinstance(opts, list):
        f["options"] = [
            (dict(o, label=_traduit(f"type.{type_}.cfg.{cle}.opt.{o.get('value')}",
                                    o.get("label") or str(o.get("value"))))
             if isinstance(o, dict) else o)
            for o in opts
        ]
    return f


def config_scope_keys(type_, scope):
    """Clés du config_schema portant ce `scope`. Chaque champ peut déclarer
    `scope: "system"` (défaut — structurel, palette Containers, admin) ou
    `scope: "user"` (réglage d'exploitation — page plugin, permission plugins.operate)."""
    m = REGISTRY.get(type_) or {}
    return {f.get("key") for f in (m.get("config_schema") or [])
            if f.get("key") and (f.get("scope") or "system") == scope}


def cles_sans_redeploiement(type_):
    """Clés du config_schema qui n'exigent PAS de redéployer le conteneur.

    ★ POURQUOI CE DRAPEAU EXISTE. Écrire un réglage de plugin redéploie le conteneur — ce qui est
    juste pour ce que le conteneur LIT à son démarrage (un format de sortie, une taille de
    canevas). Mais certains réglages ne sont jamais lus par le conteneur : ils sont lus par
    l'ORCHESTRATEUR. Le niveau de tally d'un plugin en est le cas type — c'est le distributeur
    TSL qui le consulte, dans `deploy_config`, à chaque tour. Redéployer pour ça, c'est couper un
    flux vidéo pour changer une case à cocher.

    ⚠ ET ÇA FAISAIT PERDRE DES RÉGLAGES. Le redéploiement est SÉRIALISÉ : un second appel pendant
    qu'un premier est en vol repart en 409. Un contrôle qui s'édite par gestes successifs (une
    sélection multiple) envoyait donc trois écritures dont deux étaient refusées — perdues, avec
    la valeur affichée à l'écran comme si elle avait pris. Signalé le 2026-09-01.

    Un champ déclare `"redeploiement": false` dans son `config_schema`. Le défaut reste `true` :
    c'est le cas sûr, et un réglage qu'on croit à chaud alors que le conteneur le lit au
    démarrage ne prendrait effet qu'au prochain déploiement, sans que rien ne le dise."""
    m = REGISTRY.get(type_) or {}
    return {f.get("key") for f in (m.get("config_schema") or [])
            if f.get("key") and f.get("redeploiement") is False}


def control_post_endpoints(m):
    """Endpoints de contrôle POSTables d'un manifeste, NORMALISÉS en
    [{path, port, desc}]. Tolère les deux formes de `control.endpoints` :
    liste de chemins (forme courante), ou dict {nom: {path, method, port, desc}}
    (forme 2110_io). Consommé par le catalogue générique (8e passe ch.6) et
    l'étape `post` du moteur de macros."""
    ctrl = (m or {}).get("control") or {}
    eps = ctrl.get("endpoints") or []
    default_port = int(ctrl.get("port") or 8082)
    out = []
    if isinstance(eps, dict):
        for name, spec in eps.items():
            spec = spec or {}
            if (spec.get("method") or "POST").upper() != "POST":
                continue
            out.append({"path": spec.get("path") or ("/" + name),
                        "port": int(spec.get("port") or default_port),
                        "desc": spec.get("desc") or ""})
    else:
        out = [{"path": p, "port": default_port, "desc": ""} for p in eps]
    return out


def manifest_summary_for_js():
    """Résumé compact des manifestes pour window.MXL_PLUGINS.
    Expose uniquement les métadonnées UI : label, badge, nav.section, et les champs
    optionnels reference_slot et skaarhoj. Aucun chemin ni code plugin n'est inclus."""
    out = {}
    for t, m in REGISTRY.items():
        badge = m.get("badge") or {}
        nav   = m.get("nav") or {}
        skh   = m.get("skaarhoj") or {}
        entry = {
            "label":       libelle_type(t),
            "badge_class": badge.get("class") or "",
            # Le badge porte souvent une forme COURTE distincte du label (« SONDE », « AV Sync ») :
            # il a donc sa propre clé, avec repli sur le badge du manifeste puis sur le label.
            "badge_label": _traduit(f"type.{t}.badge", badge.get("label") or libelle_type(t)),
            "badge_oklch": badge.get("oklch") or "",
            "nav_section": nav.get("section") or "",
        }
        rs = m.get("reference_slot")
        if rs is not None:
            entry["reference_slot"] = int(rs)
        # aggregate_fps=false → un fps de carte agrégé n'a pas de sens (moteur multi-flux) :
        # la page Câbles masque le badge fps de carte et affiche le fps par port à la place.
        if m.get("aggregate_fps") is False:
            entry["aggregate_fps"] = False
        skh_mode = skh.get("mode") or ""
        if skh_mode:
            entry["skaarhoj_mode"]      = skh_mode
            entry["skaarhoj_desc_i18n"] = skh.get("desc_i18n") or ""
        out[t] = entry
    return out


def badge_css_vars():
    """{badge_class: oklch} dédupliqué pour générer le bloc <style> badge dans layout.html.
    Seuls les plugins qui déclarent badge.oklch sont inclus."""
    seen = {}
    for m in REGISTRY.values():
        badge = m.get("badge") or {}
        cls   = badge.get("class") or ""
        oklch = badge.get("oklch") or ""
        if cls and oklch and cls not in seen:
            seen[cls] = oklch
    return seen


# Types qui déclarent un canvas (width/height) mais dont le format N'EST PAS celui de la maison :
# il est imposé par la SOURCE. `stream_in` ingère un flux externe — lui coller le format système
# serait un mensonge, pas un défaut utile.
_FORMAT_NOT_FROM_SETTINGS = ("stream_in",)


def effective_deploy_defaults(type_):
    """`deploy_defaults` du manifeste, avec le format vidéo (width/height/fps/scan) remplacé par le
    format par défaut SYSTÈME des Réglages (get_default_video_format) — jamais le littéral 720p du
    plugin.json. Piège connu : canvas mixer 720p + sources 1080 → inputs silencieusement non liés.
    Même principe que le filet multiview de render_script : le défaut d'un format de composition/
    génération vient des Réglages, l'explicite (POST/persisté) prime toujours
    (merge défauts ← existants ← POST inchangé côté appelants).

    ⚠ C'était une LISTE BLANCHE en dur ("mixer", "avsync") → tout type ajouté ensuite retombait
    silencieusement sur le 720p25 de son manifeste. Vécu : un `split` créé en 720p25 alors que la
    maison est en 1080p50 (mêmes conséquences que le piège mixer ci-dessus). La règle est donc
    désormais DÉCLARATIVE : **tout type qui déclare un canvas (width+height dans ses défauts) suit
    le format système**, sauf exclusion explicite et justifiée (_FORMAT_NOT_FROM_SETTINGS) — un
    nouveau plugin est couvert sans qu'on ait à penser à l'inscrire quelque part.

    Porte aussi le MODE TRANCHE GLOBAL (setting slice_mode_global, Réglages → Vidéo)."""
    dd = dict((REGISTRY.get(type_) or {}).get("deploy_defaults") or {})
    _canvas = ("width" in dd and "height" in dd) and type_ not in _FORMAT_NOT_FROM_SETTINGS
    if _canvas:
        try:
            from .scripts import get_default_video_format
            f = get_default_video_format()
            dd.update({"width": int(f["width"]), "height": int(f["height"]), "fps": f["fps"]})
            # chroma/profondeur suivent la MÊME règle que la géométrie : un plugin qui déclare
            # ces clés les tient des Réglages, sinon on aurait corrigé la moitié du format.
            if "scan" in dd:
                dd["scan"] = f.get("scan") or "p"
            if "chroma" in dd:
                dd["chroma"] = f.get("chroma") or "422"
            if "bit_depth" in dd:
                dd["bit_depth"] = int(f.get("bit_depth") or 10)
        except Exception:
            pass
    # Mode tranche GLOBAL : si le setting slice_mode_global est actif ET que le config_schema du
    # type porte slice_mode → défaut effectif slice_mode=True (+ cadence "flow" UNIQUEMENT pour
    # les types dont le schema porte cadence). L'explicite (POST/params persistés) PRIME toujours
    # (merge défauts ← existants ← POST aux 3 points d'entrée : création, /deploy, /plugin_config).
    #
    # ★ `gpu_slice` EST POSÉ AUSSI depuis le 2026-08-11, et c'est un correctif, pas un élargissement.
    # Il en était exclu (« décision séparée, banc gate GPU ») à une époque où le GPU n'avait pas de
    # verdict. Conséquence : sur un nœud GPU, `SLICE_ON` exige les DEUX — activer le réglage global
    # laissait donc tout mur GPU SILENCIEUSEMENT en image entière. On croyait avoir tranché la
    # chaîne, un maillon ne l'était pas, et le bénéfice disparaissait partout sans aucun message :
    # exactement le scénario de la pyramide monolithique, reproduit par le réglage censé l'empêcher.
    # Verdict obtenu depuis, MESURÉ EN PRODUCTION (mur 906, Quadro P5000, 1920x1080p50) : 50,0 fps
    # tenus, zéro trame perdue, 13 ms de coût propre pour 20 de budget. Un mur qui n'en voudrait pas
    # se pose `gpu_slice: false` explicitement — l'explicite prime, c'est tout l'intérêt du merge.
    try:
        schema_keys = {f.get("key") for f in (REGISTRY.get(type_) or {}).get("config_schema") or []}
        if "slice_mode" in schema_keys:
            from . import settings as _st
            if str(_st.get("slice_mode_global") or "").strip().lower() in ("1", "true", "yes", "on"):
                dd["slice_mode"] = True
                if "gpu_slice" in schema_keys:
                    dd["gpu_slice"] = True
                if "cadence" in schema_keys:
                    dd["cadence"] = "flow"
    except Exception:
        pass
    return dd


def validate_config(type_, params):
    """Vérifie `params` contre le `config_schema` du manifeste — GARDE-FOU D'ENTRÉE,
    GÉNÉRIQUE À TOUS LES PLUGINS. Renvoie la liste (éventuellement vide) des violations,
    en français, prête à être renvoyée en 400 à l'appelant.

    Règle du projet : une valeur hors bornes est REFUSÉE avec un message clair — jamais
    écrêtée en silence (le POST était accepté, `coerce_config` rabotait, l'exploitant ne
    voyait rien : c'est exactement le bug « mélangeur réglé à 20 entrées, 8 appliquées »).
    Appelé sur les params POSTÉS (création, /deploy, /plugin_config) — PAS sur les params
    déjà persistés (une valeur héritée hors bornes ne doit pas bloquer un redéploiement :
    elle est écrêtée par coerce_config, qui lève alors une ALERTE, cf. _signaler_ecretage).
    """
    schema = (REGISTRY.get(type_) or {}).get("config_schema") or []
    p = params or {}
    errs = []
    for f in schema:
        k = f.get("key")
        if not k or k not in p:
            continue
        v, t, lbl = p[k], f.get("type"), (f.get("label") or f.get("key"))
        # RÉGLAGE NULLABLE : un champ dont le manifeste déclare `"default": null` a un état
        # « non réglé » qui est une VALEUR, pas une valeur manquante — « ligne mesurée » vide
        # veut dire « toutes les lignes ». Sans cette exception, ce champ pouvait être RÉGLÉ
        # mais jamais REMIS À VIDE : le POST était refusé « n'est pas un nombre », et un
        # conteneur restait bloqué sur la ligne 0 à chaque redéploiement — un waveform d'une
        # seule ligne, qui ressemble à un waveform en panne.
        if v in (None, "") and "default" in f and f["default"] is None:
            continue
        if t == "number":
            try:
                x = float(v)
            except (TypeError, ValueError):
                errs.append(f"« {lbl} » : « {v} » n'est pas un nombre.")
                continue
            if f.get("min") is not None and x < float(f["min"]):
                errs.append(f"« {lbl} » = {v} : valeur minimale {f['min']}.")
            if f.get("max") is not None and x > float(f["max"]):
                errs.append(f"« {lbl} » = {v} : valeur maximale {f['max']}.")
        elif t == "multiselect":
            # ★ UNE LISTE, ET LE CAS « UN SEUL » N'EN EST QUE LE PREMIER ÉLÉMENT. Le tally se
            # CUMULE : une même source peut être suivie sur plusieurs chaînes de destination, et
            # un champ scalaire obligeait à choisir laquelle compte. On tolère un scalaire en
            # entrée (schémas hérités) plutôt que de refuser un réglage déjà en base.
            _vals = v if isinstance(v, list) else ([] if v in (None, "", 0, "0") else [v])
            _opts = f.get("options") or (options_dynamiques(f["options_from"])
                                         if f.get("options_from") else [])
            _ok = {str(o.get("value")) for o in _opts}
            _hors = [x for x in _vals if _ok and str(x) not in _ok]
            if _hors:
                errs.append(f"« {lbl} » : {_hors} hors des choix possibles.")
        elif t == "select":
            # Même résolution que côté affichage : sans elle, un select à options
            # dynamiques n'aurait AUCUN garde-fou — le champ le plus permissif du
            # schéma serait celui dont la liste est la mieux définie.
            _opts = f.get("options") or (options_dynamiques(f["options_from"])
                                         if f.get("options_from") else [])
            opts = [o.get("value") for o in _opts]
            if opts and v not in opts:
                errs.append("« %s » = « %s » : valeurs admises %s." % (lbl, v, ", ".join(map(str, opts))))
    return errs


# Écrêtages déjà signalés (type, clé, valeur brute) — une alerte par valeur fautive, pas par
# appel (coerce_config est appelé à chaque déploiement / rendu de script).
_ECRETAGES_SIGNALES = set()

def _signaler_ecretage(type_, key, brut, corrige, label):
    """Un écrêtage a eu lieu (valeur PERSISTÉE hors bornes, héritée d'avant le durcissement
    ou d'un chemin non validé) → log + ALERTE. Jamais silencieux."""
    cle = (type_, key, repr(brut), repr(corrige))
    if cle in _ECRETAGES_SIGNALES:
        return
    _ECRETAGES_SIGNALES.add(cle)
    log.warning("coerce_config: %s : réglage « %s » = %s hors bornes du plugin → appliqué à %s",
                type_, label, brut, corrige)
    try:
        from .containers import ajouter_alerte
        ajouter_alerte("alert.deploy.plugin_ecretage", "warning",
                       params={"type": type_, "label": label, "brut": brut, "corrige": corrige})
    except Exception:
        pass


def coerce_config(type_, params):
    """Coerce/borne `params` selon le `config_schema` du manifeste (optionnel).
    Types : number (min/max, int si entier), checkbox (bool), select (option valide),
    text/textarea (str). Les clés hors schéma sont laissées telles quelles.

    Filet de sécurité de SORTIE (les valeurs entrantes sont refusées en amont par
    `validate_config`) : tout écrêtage effectif lève une ALERTE (`_signaler_ecretage`)."""
    m = REGISTRY.get(type_) or {}
    schema = m.get("config_schema") or []
    if not schema:
        return params
    out = dict(params or {})
    for f in schema:
        k = f.get("key")
        if not k or k not in out:
            continue
        t = f.get("type")
        v = out[k]
        # Pendant du test de `validate_config` : un réglage déclaré nullable revient à
        # l'ABSENCE, il n'est pas rabattu sur une borne. `float(None or 0)` valait 0, donc le
        # minimum — et « aucune ligne choisie » devenait « ligne 0 », silencieusement.
        if v in (None, "") and "default" in f and f["default"] is None:
            out.pop(k, None)
            continue
        if t == "number":
            brut = v
            try: v = float(v)
            except (TypeError, ValueError): v = float(f.get("default") or 0)
            if f.get("min") is not None: v = max(float(f["min"]), v)
            if f.get("max") is not None: v = min(float(f["max"]), v)
            out[k] = int(v) if float(v).is_integer() else v
            try:
                ecrete = float(brut) != float(out[k])
            except (TypeError, ValueError):
                ecrete = True
            if ecrete:
                _signaler_ecretage(type_, k, brut, out[k], f.get("label") or k)
        elif t in ("checkbox", "bool"):
            # "bool" est un alias de "checkbox". Attention : bool("False") == True →
            # on interprète les chaînes explicitement.
            if isinstance(v, str):
                out[k] = v.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[k] = bool(v)
        elif t == "multiselect":
            # Normalisation de SORTIE : toujours une liste, dédoublonnée, ordre préservé. Les
            # consommateurs (distributeur TSL, hooks tally_targets) n'ont ainsi qu'une forme à
            # lire — un scalaire hérité y devient la liste à un élément, sans resave.
            #
            # ⚠ ON NE CONVERTIT PAS EN ENTIER. Les valeurs sont des identités opaques (UUID de
            # niveau) depuis le 2026-09-01 : `int()` les jetterait toutes, et le champ se
            # viderait à chaque enregistrement sans le moindre message.
            brut = v if isinstance(v, list) else ([] if v in (None, "", 0, "0") else [v])
            vus, propre = set(), []
            for x in brut:
                x = str(x or "").strip()
                if x and x not in vus:
                    vus.add(x)
                    propre.append(x)
            out[k] = propre
        elif t == "select":
            opts = [o.get("value") for o in (f.get("options") or [])]
            if opts and v not in opts:
                out[k] = f.get("default") if f.get("default") in opts else opts[0]
                _signaler_ecretage(type_, k, v, out[k], f.get("label") or k)
        else:  # text / textarea
            out[k] = "" if v is None else str(v)
    return out


def _port_format(essence, params):
    """Descripteur de format d'un port, dérivé best-effort des params déjà persistés.
    Source de vérité unique = deploy_config.params ; jamais bloquant (renvoie None si
    indéterminable). Consommé par /api/home/summary → page Câbles / Monitoring.
    - video → {width, height, fps, chroma, pix_fmt}
    - audio → {sample_rate, bit_depth, channels}"""
    try:
        from .scripts import (normalize_chroma, PIX_FMT_BY_CHROMA, normalize_bit_depth,
                              DEFAULT_COLORIMETRY)
        p = params or {}
        v = p.get("video") if isinstance(p.get("video"), dict) else {}
        if essence == "video":
            # Fallback sur input_format (format de la source câblée, stocké par _apply_wire)
            # pour les plugins passthrough (delay, avsync) qui n'ont pas width/height propres.
            inf = p.get("input_format") or {}
            chroma = normalize_chroma(p.get("chroma") or v.get("chroma") or inf.get("chroma"))
            # out_width/out_height AVANT width/height : pour un multiview c'est out_width qui pilote
            # la sortie réelle (script.py : OUT_WIDTH = out_width or width). Les lire après width
            # faisait afficher width (canevas voulu) sur la page Câbles alors que le flux émis suivait
            # out_width → mismatch au câblage TX. Seul le multiview porte out_width (streamer ne produit
            # aucun shm), donc les autres types retombent sur width sans changement.
            w = int(v.get("width") or p.get("out_width") or p.get("width") or inf.get("width") or 0)
            h = int(v.get("height") or p.get("out_height") or p.get("height") or inf.get("height") or 0)
            # Multiview portrait : le flux émis est tourné 90° → dims SWAPPÉES (cf scripts.multiview_output_dims).
            if str(p.get("orientation") or "").strip().lower() in ("portrait_cw", "portrait_ccw"):
                w, h = h, w
            fps = v.get("fps") or p.get("fps") or inf.get("fps")
            fmt = {"chroma": chroma, "pix_fmt": PIX_FMT_BY_CHROMA[chroma],
                   "bit_depth": normalize_bit_depth(v.get("bit_depth") or p.get("bit_depth") or inf.get("bit_depth"))}
            colo = p.get("colorimetry") or v.get("colorimetry") or inf.get("colorimetry")
            fmt["colorimetry"] = str(colo or DEFAULT_COLORIMETRY).strip().lower()
            if w: fmt["width"] = w
            if h: fmt["height"] = h
            if fps:
                # Cadence EXACTE (num/den) propagée pour le fractionnaire (29.97/59.94) ; `fps`
                # garde la valeur précise (29.97), `fps_num`/`fps_den` la forme rationnelle exacte
                # (consommée par l'UDC et le gating cadence #27).
                num, den = rate_nd(fps)
                fmt["fps_num"] = num; fmt["fps_den"] = den
                fmt["fps"] = num if den == 1 else round(num / den, 3)   # 25 (int) / 29.97 (float)
            # Balayage (entrelacé/progressif) + ordre de champ : propagés au consommateur câblé pour
            # qu'une sortie 2110 ré-émette dans le bon format (ex. 1080i50 → scan=i, sinon 25p +
            # combing). Source de vérité = params.scan/field_order (jamais l'étiquette du flux MXL,
            # cf. interlace_mode forcé progressive côté grain). Best-effort, omis si inconnu.
            scan = v.get("scan") or p.get("scan") or inf.get("scan")
            if scan:
                fmt["scan"] = "i" if str(scan).strip().lower() == "i" else "p"
            fo = v.get("field_order") or p.get("field_order") or inf.get("field_order")
            if fo:
                fmt["field_order"] = "bff" if str(fo).strip().lower() == "bff" else "tff"
            return fmt
        if essence == "audio":
            a = p.get("audio") if isinstance(p.get("audio"), dict) else {}
            ch = a.get("channels") or p.get("audio_channels") or 8
            try: ch = int(ch)
            except (TypeError, ValueError): ch = 8
            return {"sample_rate": 48000, "bit_depth": 24, "channels": ch}
    except Exception:
        return None
    return None


def ports_topologie(type_, hostname, params=None):
    """Ports d'un conteneur — {"produces": [{shm, kind}], "consumes": [{shm, kind}]} — TELS QUE LE
    CÂBLAGE LES VOIT. **Source unique** de la question « qui produit / qui consomme ce flux ».

    ★ POURQUOI CETTE FONCTION EXISTE. Il y avait DEUX façons de répondre à cette question, et
    elles se contredisaient :

      • `services/rdma._cablage_flotte` passait par le hook `topology_ports` du plugin, qui
        résout les `state_field` — donc il VOYAIT le câble multiview → TX0 du moteur 2110 ;
      • `cabling._flow_consumers_on_node` lisait `derive_wiring().consumes[].shm` brut, que le
        manifeste laisse à None quand le flux vit dans un `state_field` — donc il ne voyait
        AUCUN émetteur 2110.

    Résultat, le 2026-08-09 : la réconciliation créait le lien RDMA parce que le câble existait,
    la purge le supprimait 127 s plus tard parce que « personne ne consomme », et la boucle a
    tourné des heures — une sortie 2110 de production sans image, sans un message. Le commentaire
    accompagnant la purge affirmait pourtant « les deux prédicats sont le même, donc ils ne
    peuvent pas se contredire ».

    D'où la règle, désormais tenue par la structure et non par la vigilance : **une notion, une
    seule fonction.** Les deux appelants passent par ici ; diverger redevient impossible.

    Ordre de résolution — le hook `topology_ports` du plugin d'abord (il connaît ses cas
    particuliers), sinon `derive_wiring` AVEC résolution des `state_field`."""
    params = params or {}
    try:
        hook = get_hook(type_, "topology_ports")
    except Exception:                                                      # noqa: BLE001
        hook = None
    if hook:
        try:
            ports = hook(hostname or "", params, {}) or {}
            return {"produces": list(ports.get("produces") or []),
                    "consumes": list(ports.get("consumes") or [])}
        except Exception:                                                  # noqa: BLE001
            pass          # hook cassé → on retombe sur le wiring déclaratif, jamais sur rien
    if not is_plugin(type_):
        return {"produces": [], "consumes": []}
    try:
        w = derive_wiring(type_, hostname or "", params)
    except Exception:                                                      # noqa: BLE001
        return {"produces": [], "consumes": []}

    def _shm(x):
        # `shm` peut être porté par un `state_field` : le flux câblé vit alors dans les params
        # (ex. `tx0_shm`), parce qu'il change à chaud sans redéploiement. C'est la résolution
        # que `deploy.py` fait déjà — l'oublier rendait des ports entiers invisibles.
        v = x.get("shm")
        if not v and x.get("state_field"):
            v = params.get(x["state_field"]) or ""
        return v

    return {"produces": [{"shm": _shm(x), "kind": x.get("essence") or "video"}
                         for x in (w.get("produces") or []) if _shm(x)],
            "consumes": [{"shm": _shm(x), "kind": x.get("essence") or "video"}
                         for x in (w.get("consumes") or []) if _shm(x)]}


def derive_wiring(type_, hostname, params=None):
    """Resolve manifest.wiring produces/consumes against hostname + deploy params.
    - `{hostname}` ET les clés de `params` sont substitués dans les `shm` (ex. `"{shm_out}"`
      → params["shm_out"]). Clé absente → laissée telle quelle.
    - `"repeat": "<param_key>"` : déplie `params[param_key]` fois, substitution `{i}`/`{i1}`,
      `slot`=index. Entrées identiques indexées (mixer).
    - `"from_list": "<param_key>"` : déplie sur la **liste** `params[param_key]` (entrées à
      géométrie, ex. multiview flux_config). Par entrée : `slot`=index, `shm` lu de
      `entry[shm_field]` (préfixe `shm_prefix` retiré ; vide → port libre), `label` de
      `entry[name_field]` ou « Entrée {i+1} » ; les clés `from_list/shm_field/shm_prefix/
      name_field/dims_fields/input_key/existing_only` sont propagées (pour le câblage).
    Returns {"produces": [...], "consumes": [...], "mode": str, "state_endpoint": str}."""
    m = REGISTRY.get(type_) or {}
    w = dict(m.get("wiring") or {})
    params = params or {}

    def _safe_fmt(s):
        """Substitue {hostname} + {clés de params} dans une string, sans planter sur une
        clé absente."""
        if not isinstance(s, str):
            return s
        import string
        mapping = {"hostname": hostname}
        mapping.update({k: v for k, v in params.items() if isinstance(v, (str, int, float))})
        out = s
        for k, v in mapping.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _fmt(v, **kw):
        return v.format(**kw) if isinstance(v, str) else v

    def _with_format(copy):
        """Attache un descripteur `format` aux ports video/audio (dérivé des params)."""
        ess = copy.get("essence")
        if ess in ("video", "audio") and "format" not in copy:
            fmt = _port_format(ess, params)
            if fmt:
                copy["format"] = fmt
        return copy

    def _libelle_port(sens, idx, gabarit):
        """Libellé de PORT traduit. Comme les libellés de type et les schémas de config, il vit
        dans le manifeste en français (« Source vidéo », « Entrée V{i1} ») et s'affichait tel quel
        sur la page Câbles quelle que soit la langue (signalé en recette le 2026-08-24).

        Convention : `type.<id>.wire.<produces|consumes>.<index du manifeste>`. L'index est stable
        — le réordonner casserait déjà l'affectation des slots. Clé absente → gabarit du manifeste.
        Le gabarit garde ses `{i1}` : la substitution a lieu APRÈS, comme avant."""
        if not gabarit:
            return gabarit
        return _traduit(f"type.{type_}.wire.{sens}.{idx}", gabarit)

    def _resolve(items, sens=""):
        out = []
        for idx, it in enumerate(items or []):
            it = dict(it)
            if it.get("label"):
                it["label"] = _libelle_port(sens, idx, it["label"])
            if "repeat" in it:
                try: n = int(params.get(it["repeat"]) or 0)
                except (TypeError, ValueError): n = 0
                tpl = {k: v for k, v in it.items() if k != "repeat"}
                for i in range(max(0, n)):
                    copy = {k: _fmt(v, hostname=hostname, i=i, i1=i + 1) for k, v in tpl.items()}
                    copy["slot"] = i
                    out.append(_with_format(copy))
            elif "from_list" in it:
                lst = params.get(it["from_list"]) or []
                shm_field = it.get("shm_field", "shm")
                prefix    = it.get("shm_prefix", "")
                name_field = it.get("name_field")
                for i, entry in enumerate(lst):
                    entry = entry if isinstance(entry, dict) else {}
                    raw = entry.get(shm_field) or ""
                    if prefix and raw.startswith(prefix):
                        raw = raw[len(prefix):]
                    copy = {k: v for k, v in it.items() if k != "from_list"}
                    copy["from_list"] = it["from_list"]
                    copy["slot"] = i
                    copy["shm"] = raw
                    # Libellé EXPLICITE du manifeste (gabarit `{i1}`) prioritaire : une même liste
                    # à géométrie peut porter PLUSIEURS essences par entrée (multiview : la vidéo
                    # et son flux ANC) — sans ça les deux ports s'appelleraient « Entrée N ».
                    lbl_tpl = it.get("label")
                    copy["label"] = (_fmt(lbl_tpl, hostname=hostname, i=i, i1=i + 1) if lbl_tpl
                                     else (entry.get(name_field) if name_field else None)
                                     or _traduit("js.wire.default_input", "Entrée {i1}").replace("{i1}", str(i + 1)))
                    out.append(_with_format(copy))
            else:
                copy = dict(it)
                if copy.get("shm"):
                    copy["shm"] = _safe_fmt(copy["shm"])
                out.append(_with_format(copy))
        return out

    produces = _resolve(w.get("produces"), "produces")
    consumes = _resolve(w.get("consumes"), "consumes")

    # ── Pyramide : produces DYNAMIQUES = 1 proxy par (source câblée × niveau). Les sorties
    # ne sont pas déclarables en statique (elles dépendent de ce qui est câblé) → on les dérive
    # ici depuis input_0..input_{n-1} (+ input_{i}_fmt pour les dimensions/chroma). Le nommage
    # <source>__pL ET la formule de dimensions sont les MÊMES que plugins/pyramide/script.py.
    # ── Scope : un flux PAR INSTRUMENT publié ────────────────────────────────────────────
    # Comme la pyramide, ces sorties ne sont pas déclarables en statique : elles dépendent de ce
    # que l'exploitant a demandé. Le NOM porte l'instrument (`<hôte>_scope_waveform`) et non un
    # numéro — sur la page Câbles, « Sortie 2 » n'apprendrait rien à personne.
    if type_ == "scope":
        # ⚠ LE FORMAT DÉCLARÉ DES SORTIES EST CELUI DE LA SORTIE, pas celui de la source.
        # `_port_format` dérive des params `width`/`height`, c'est-à-dire du flux MESURÉ : sans
        # cette reprise, un multiview lirait « 1920×1080 » pour une mosaïque réellement produite
        # en 960×540, et dimensionnerait sa case sur un chiffre faux.
        def _fmt_sortie(l, h):
            base = dict(_port_format("video", params) or {})
            if l and h:
                base["width"], base["height"] = int(l), int(h)
            return base or None

        _lo = int(params.get("out_largeur") or 0) or int(params.get("width") or 0)
        _ho = int(params.get("out_hauteur") or 0) or int(params.get("height") or 0)
        for pr in produces:
            if pr.get("shm") == "%s_scope" % hostname:
                f = _fmt_sortie(_lo - (_lo % 2) if _lo else 0, _ho)
                if f:
                    pr["format"] = f
        _ls = int(params.get("sorties_largeur") or 960)
        _hs = int(params.get("sorties_hauteur") or 540)
        for i, ins in enumerate(params.get("sorties") or []):
            if not isinstance(ins, str) or not ins:
                continue
            entree = {"essence": "video", "slot": 100 + i,
                      "shm": "%s_scope_%s" % (hostname, ins),
                      "label": "Sortie %s" % ins}
            f = _fmt_sortie(_ls, _hs)
            if f:
                entree["format"] = f
            produces.append(_with_format(entree))

    if type_ == "pyramide":
        try: n_inputs = int(params.get("n_inputs") or 8)
        except (TypeError, ValueError): n_inputs = 8
        # Socle d'octaves : MÊME dérivation que plugins/pyramide/script.py (base_octaves, override
        # `levels`). full ½/¼/⅛/1/16 · half ½ · none aucun.
        if params.get("levels"):
            levels = [int(x) for x in params["levels"]]
        else:
            levels = {"full": [2, 4, 8, 16], "half": [2], "none": []}.get(
                str(params.get("base_octaves") or "full").lower(), [2, 4, 8, 16])
        extra = params.get("extra_sizes") if isinstance(params.get("extra_sizes"), dict) else {}
        prod = []
        seen_by_src = {}
        for i in range(n_inputs):
            src = params.get(cle_input(i))
            if not src:
                continue
            fmt = params.get(cle_input(i, fmt=True)) or {}
            w_, h_ = int(fmt.get("width") or 0), int(fmt.get("height") or 0)
            chroma = fmt.get("chroma") or "422"
            cw = {"420": 2, "422": 2, "444": 1}.get(chroma, 2)
            ch = {"420": 2, "422": 1, "444": 1}.get(chroma, 1)

            def _pfmt(pw, ph):
                d = {"chroma": chroma, "width": pw, "height": ph}
                if fmt.get("bit_depth"): d["bit_depth"] = fmt["bit_depth"]
                if fmt.get("pix_fmt"): d["pix_fmt"] = fmt["pix_fmt"]
                if fmt.get("colorimetry"): d["colorimetry"] = fmt["colorimetry"]
                return d

            seen = seen_by_src.setdefault(src, set())
            for L in levels:
                p = {"essence": "video", "shm": f"{src}__p{L}",
                     "label": f"{src} 1/{L}", "slot": i, "level": L}
                if w_ and h_:
                    pw = max(2, w_ // L); pw -= pw % max(2, cw)
                    ph = max(2, h_ // L); ph -= ph % max(2, ch)
                    seen.add((pw, ph)); p["format"] = _pfmt(pw, ph)
                prod.append(p)
            # Tailles sur-mesure (mêmes nom/alignement que plugins/pyramide/script.py:_align_extra).
            for wh in (extra.get(src) or []):
                try:
                    rw, rh = int(wh[0]), int(wh[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not (w_ and h_):
                    continue
                pw = min(max(2, rw), w_); pw -= pw % max(2, cw)
                ph = min(max(2, rh), h_); ph -= ph % max(2, ch)
                if pw < 2 or ph < 2 or (pw, ph) in seen:
                    continue
                seen.add((pw, ph))
                prod.append({"essence": "video", "shm": f"{src}__s{pw}x{ph}",
                             "label": f"{src} {pw}×{ph}", "slot": i, "format": _pfmt(pw, ph)})
        produces = prod

    return {
        "produces": produces,
        "consumes": consumes,
        "mode": w.get("mode", "redeploy"),
        # endpoint :8082 renvoyant l'état live (shm câblés) pour les consumes hot-wire
        "state_endpoint": w.get("state_endpoint", "/state"),
    }


# ─── Import / export / activation de paquets .mxlplugin ─────────────
_SAFE_TYPE = re.compile(r"^[A-Za-z0-9_-]+$")


def _ver_key(v):
    """Clé de comparaison de version : numérique par segment, fallback lexical (en dernier)."""
    try:
        return (0, tuple(int(x) for x in str(v).split(".")))
    except (ValueError, AttributeError):
        return (1, (str(v),))


def validate_package(d):
    """Valide un dossier de plugin extrait. Retourne (manifest, None) ou (None, raison).
    Ne fait que LIRE + un dry-run `str.format` (aucune exécution de code plugin)."""
    mp = os.path.join(d, "plugin.json")
    if not os.path.isfile(mp):
        return None, "plugin.json manquant"
    try:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:
        return None, f"plugin.json invalide : {e}"
    missing = [k for k in REQUIRED_KEYS if k not in man]
    if missing:
        return None, f"clés manquantes : {', '.join(missing)}"
    if not _SAFE_TYPE.match(str(man.get("type", ""))):
        return None, "type invalide (autorisé : lettres, chiffres, _ et -)"
    sp = os.path.join(d, man["script_template"])
    if not os.path.isfile(sp):
        return None, f"script {man['script_template']} manquant"
    try:
        with open(sp, encoding="utf-8") as f:
            f.read().format(config="{}", hostname="x", plugin_version="0")
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
        # `{config[x]}` → TypeError, `{config.attr}` → AttributeError : couvrir tous les
        # cas d'accolade non doublée (même élargissement que le dry-run de _scan()).
        return None, f"accolade littérale non doublée dans {man['script_template']} ({e})"
    return man, None


def export_dir(type_):
    """Dossier du plugin à zipper pour l'export (None si type inconnu)."""
    m = REGISTRY.get(type_)
    return m["_dir"] if m else None


def export_version_dir(type_, version):
    """Dossier source pour zipper UNE version : dossier plat si version courante (le
    zip devra exclure le sous-dossier versions/), sinon versions/<ver>/.
    Retourne (dir, version) ou (None, None) si la version est inconnue."""
    m = REGISTRY.get(type_)
    if not m or version not in versions(type_):
        return None, None
    if version == m.get("version"):
        return m["_dir"], version
    return os.path.join(m["_dir"], "versions", version), version


def stamp_imported_at(src_dir):
    """Tamponne imported_at (maintenant, ISO secondes) dans src_dir/meta.json (créé si
    absent), avant installation. Appelé sur les imports (plugin entier ou version)."""
    import datetime
    p = os.path.join(src_dir, "meta.json")
    data = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            data = {}
    data["imported_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _flat_names(d):
    """Entrées « plates » d'un dossier de plugin (tout sauf le sous-dossier versions/)."""
    return [n for n in os.listdir(d) if n != "versions"]


def _copy_into(src_dir, dst_dir, names):
    os.makedirs(dst_dir, exist_ok=True)
    for n in names:
        s = os.path.join(src_dir, n)
        d = os.path.join(dst_dir, n)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def _clear_flat(d):
    for n in _flat_names(d):
        p = os.path.join(d, n)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def _archive_current(tdir, cur_ver):
    """Archive les fichiers plats courants vers versions/<cur_ver>/ (no-op si déjà présent)."""
    if not cur_ver:
        return
    dest = os.path.join(tdir, "versions", cur_ver)
    if os.path.isdir(dest):
        return
    _copy_into(tdir, dest, _flat_names(tdir))


def _merge_versions(src_dir, tdir):
    """Recopie les versions/ importées absentes de la cible (ne touche pas aux existantes)."""
    vsrc = os.path.join(src_dir, "versions")
    if not os.path.isdir(vsrc):
        return
    for v in os.listdir(vsrc):
        s = os.path.join(vsrc, v)
        d = os.path.join(tdir, "versions", v)
        if os.path.isdir(s) and not os.path.isdir(d):
            shutil.copytree(s, d)


def install_package(src_dir, *, activate):
    """Installe un paquet validé (dossier `src_dir`) dans PLUGINS_DIR/<type>/.
    activate=True → devient la version COURANTE (archive l'ancienne d'abord).
    activate=False → rangé sous versions/<ver>/ (archivée). Recharge le registre."""
    man, err = validate_package(src_dir)
    if err:
        raise ValueError(err)
    type_, ver = man["type"], man["version"]
    tdir = os.path.join(PLUGINS_DIR, type_)
    cur = (REGISTRY.get(type_) or {}).get("version") if os.path.isdir(tdir) else None
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
    return {"type": type_, "version": ver}


def activate_version(type_, version):
    """Promeut une version archivée en version COURANTE (archive la courante d'abord).
    Si la version archivée n'a pas son plugin.json, reconstruit le manifeste courant avec
    la version forcée. Recharge le registre."""
    m = REGISTRY.get(type_)
    if not m:
        raise ValueError("type inconnu")
    tdir, cur = m["_dir"], m.get("version")
    if version == cur:
        return {"type": type_, "version": version}
    vdir = os.path.join(tdir, "versions", version)
    if not os.path.isdir(vdir):
        raise ValueError(f"version {version} introuvable")
    _archive_current(tdir, cur)
    _clear_flat(tdir)
    _copy_into(vdir, tdir, os.listdir(vdir))
    mp = os.path.join(tdir, "plugin.json")
    if not os.path.isfile(mp):                 # archive script-only → reconstruit le manifeste
        man = {k: v for k, v in m.items() if k != "_dir"}
        man["version"] = version
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
    else:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("version") != version:
            man["version"] = version
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False, indent=2)
    reload()
    return {"type": type_, "version": version}


# Initial scan at import time.
REGISTRY = _scan()
# Empreinte des manifestes au scan initial → `rescan_if_changed()` ne verra un « changement » que
# s'il y en a un vraiment (sans ça, le tout premier déploiement re-scannerait pour rien).
_FINGERPRINT.update(_manifests_fingerprint())
