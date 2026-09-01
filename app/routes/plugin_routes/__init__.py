# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Routes BESPOKE par type de plugin (persistance DB, logique métier propre à un type).

Le contrôle live générique (état, actions) passe par le proxy `/api/containers/<vmid>/
plugin/<path>` (app.routes.plugin_proxy) et n'a besoin d'AUCUN code ici — un plugin qui
se contente de déclarer `control.endpoints` dans son plugin.json n'a pas de module dans
ce paquet. Un module `<type>.py` n'est nécessaire QUE si le type a besoin de routes qui
touchent la DB (mémoires, snapshots…) au-delà du forward :8082 — cf. `split.py`.

Convention : `app/routes/plugin_routes/<type>.py` miroir de `plugins/<type>/` (nommé
`plugin_routes` et non `plugins` pour ne pas entrer en collision avec `app.plugins`,
le registre de plugins massivement importé via `from . import plugins` dans
`app/routes/__init__.py`). Chaque module importe `bp` (et les helpers dont il a besoin)
depuis `app.routes` et déclare ses routes avec `@bp.route(...)` comme n'importe quelle
route du paquet."""

from . import split  # noqa: F401 — l'import déclenche l'enregistrement des routes sur bp
