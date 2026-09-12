# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Smoke test autonome (sans pytest) : attrape les régressions de boot AVANT un restart en prod.

Vérifie, dans l'ordre :
  1. Import de tous les modules de `app/` (chaque .py à la racine du package + le sous-paquet
     `app.routes`) — une exception d'import ici = crash au boot de main.py.
  2. `app.database.init_db()` sur une DB TEMPORAIRE (jamais la DB de prod), appelé deux fois
     pour vérifier l'idempotence des migrations.
  3. Le registre de plugins (`app.plugins`) : chaque dossier `plugins/<type>/plugin.json`
     présent sur disque doit être chargé dans le REGISTRY — un plugin « skippé » (accolade
     `{`/`}` non doublée dans script.py, cf. CLAUDE.md) fait échouer le test avec son nom.
  4. `render_script()` de chaque plugin du REGISTRY avec des params par défaut
     (deploy_defaults + défauts de config_schema) : une accolade littérale non doublée qui a
     échappé au dry-run de `_scan()` (ex. dépendant d'une clé absente au dry-run) lève
     KeyError/IndexError/... ici.
  5. Import de chaque service de `services/` (un sous-dossier = un service, cf. CLAUDE.md).

Usage :
    ./venv/bin/python tests/smoke_test.py

Exit 0 si tout passe, exit 1 sinon (avec le détail des échecs sur stdout).

Contraintes respectées :
  - Ne touche JAMAIS /opt/bobistudio/db_bobistudio.db (DB temporaire via tempfile, DB_PATH
    surchargé sur le module app.database AVANT le premier init_db()).
  - Aucun accès réseau, aucun serveur Flask lancé.
  - Ne modifie aucun fichier existant du repo.
"""
import importlib
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
PLUGINS_DIR = os.path.join(ROOT, "plugins")
SERVICES_DIR = os.path.join(ROOT, "services")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES = []   # [(step, detail)]
PASSES = []     # [step]


def ok(step):
    PASSES.append(step)
    print(f"[ OK ] {step}")


def fail(step, detail=""):
    FAILURES.append((step, detail))
    print(f"[FAIL] {step}")
    if detail:
        for line in detail.rstrip().splitlines():
            print(f"        {line}")


def _import_app_modules():
    """Étape 1 : importe chaque .py de app/ (top-level) + le sous-paquet app.routes.
    Retourne le module app.database déjà importé (nécessaire pour l'étape 2)."""
    names = sorted(
        f[:-3] for f in os.listdir(APP_DIR)
        if f.endswith(".py") and not f.startswith("__")
    )
    # app.database en premier et isolément : on doit pouvoir surcharger son DB_PATH avant
    # que quoi que ce soit d'autre ne soit importé, par prudence (aucun module n'ouvre la
    # DB au niveau module d'après revue de code, mais on ne prend pas de risque).
    database_mod = None
    try:
        database_mod = importlib.import_module("app.database")
        ok("import app.database")
    except Exception:
        fail("import app.database", traceback.format_exc())
        return None

    for name in names:
        if name == "database":
            continue
        modname = f"app.{name}"
        try:
            importlib.import_module(modname)
            ok(f"import {modname}")
        except Exception:
            fail(f"import {modname}", traceback.format_exc())

    # sous-paquet routes/ (Blueprint unique, cf. CLAUDE.md)
    if os.path.isdir(os.path.join(APP_DIR, "routes")):
        try:
            importlib.import_module("app.routes")
            ok("import app.routes")
        except Exception:
            fail("import app.routes", traceback.format_exc())

    return database_mod


def _init_db_twice(database_mod, tmp_db_path):
    database_mod.DB_PATH = tmp_db_path
    try:
        database_mod.init_db()
        ok("app.database.init_db() sur DB vierge")
    except Exception:
        fail("app.database.init_db() sur DB vierge", traceback.format_exc())
        return
    try:
        database_mod.init_db()
        ok("app.database.init_db() idempotent (2e appel)")
    except Exception:
        fail("app.database.init_db() idempotent (2e appel)", traceback.format_exc())


def _plugin_disk_types():
    """{dossier: type_declaré_ou_None} pour chaque plugins/<dossier>/plugin.json présent."""
    import json
    out = {}
    if not os.path.isdir(PLUGINS_DIR):
        return out
    for name in sorted(os.listdir(PLUGINS_DIR)):
        manifest_path = os.path.join(PLUGINS_DIR, name, "plugin.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                out[name] = json.load(f).get("type")
        except Exception:
            out[name] = None
    return out


def _check_plugin_registry():
    try:
        plugins = importlib.import_module("app.plugins")
    except Exception:
        fail("import app.plugins", traceback.format_exc())
        return None

    disk = _plugin_disk_types()
    scan_errors = plugins.scan_errors()
    registry_types = set(plugins.REGISTRY.keys())

    if scan_errors:
        detail = "\n".join(f"{d}: {reason}" for d, reason in scan_errors.items())
        fail("plugins.REGISTRY : aucun plugin skippé au scan", detail)
    else:
        ok("plugins.REGISTRY : aucun plugin skippé au scan")

    missing = []
    for dirname, declared_type in disk.items():
        if dirname in scan_errors:
            continue
        expected_type = declared_type or dirname
        if expected_type not in registry_types:
            missing.append(dirname)
    if missing:
        fail("plugins.REGISTRY couvre tous les dossiers plugins/<type>/plugin.json",
             "présents sur disque mais absents du REGISTRY (ni signalés en erreur) : "
             + ", ".join(missing))
    else:
        ok("plugins.REGISTRY couvre tous les dossiers plugins/<type>/plugin.json")

    return plugins


def _default_params_for(manifest):
    """Params par défaut = deploy_defaults du manifeste, complétés par les défauts déclarés
    dans config_schema (Tier 1) pour les clés absentes."""
    params = dict(manifest.get("deploy_defaults") or {})
    for field in manifest.get("config_schema") or []:
        key = field.get("key")
        if key and key not in params and "default" in field:
            params[key] = field["default"]
    return params


def _check_render_scripts(plugins):
    if plugins is None:
        fail("render_script() de chaque plugin", "REGISTRY indisponible (étape précédente en échec)")
        return
    for type_ in sorted(plugins.REGISTRY.keys()):
        manifest = plugins.REGISTRY[type_]
        params = _default_params_for(manifest)
        try:
            rendered = plugins.render_script(type_, params, hostname="smoketest-host")
            if not rendered:
                fail(f"render_script({type_!r})", "résultat vide/None")
                continue
            ok(f"render_script({type_!r})")
        except Exception:
            fail(f"render_script({type_!r})", traceback.format_exc())


def _check_services():
    if not os.path.isdir(SERVICES_DIR):
        fail("import services/*", f"dossier introuvable : {SERVICES_DIR}")
        return
    for name in sorted(os.listdir(SERVICES_DIR)):
        d = os.path.join(SERVICES_DIR, name)
        if not os.path.isdir(d) or name.startswith("__"):
            continue
        has_marker = os.path.isfile(os.path.join(d, "__init__.py")) or \
            os.path.isfile(os.path.join(d, "manifest.json"))
        if not has_marker:
            continue
        modname = f"services.{name}"
        try:
            importlib.import_module(modname)
            ok(f"import {modname}")
        except Exception:
            fail(f"import {modname}", traceback.format_exc())


def main():
    tmp_dir = tempfile.mkdtemp(prefix="bobistudio_smoke_")
    tmp_db_path = os.path.join(tmp_dir, "smoke_test.db")
    print(f"(DB temporaire : {tmp_db_path})")
    print()
    try:
        print("== Étape 1 : import de tous les modules app/ ==")
        database_mod = _import_app_modules()

        print()
        print("== Étape 2 : app.database.init_db() sur DB temporaire (jamais la prod) ==")
        if database_mod is not None:
            _init_db_twice(database_mod, tmp_db_path)
        else:
            fail("app.database.init_db()", "module app.database non importé (étape 1 en échec)")

        print()
        print("== Étape 3 : registre de plugins (plugins/<type>/plugin.json) ==")
        plugins_mod = _check_plugin_registry()

        print()
        print("== Étape 4 : render_script() de chaque plugin avec params par défaut ==")
        _check_render_scripts(plugins_mod)

        print()
        print("== Étape 5 : import des services (services/<nom>) ==")
        _check_services()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    print("=" * 70)
    print(f"{len(PASSES)} check(s) OK, {len(FAILURES)} échec(s)")
    if FAILURES:
        print()
        print("Échecs :")
        for step, _detail in FAILURES:
            print(f"  - {step}")
        return 1
    print("Smoke test OK — pas de régression de boot détectée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
