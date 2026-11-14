#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The Medias page and the workspace without the media_manager service (2026-11-14).

On an instance where the `media_manager` service is not installed, /medias answered 500
(TemplateNotFound: media_manager/embed.html) for every user allowed to manage media: the page
included the service's template whenever the user had `media.manage`, never asking whether the
service was there. The project workspace did the same with its media manager widget. Rule set by
Cyril the same day: a page shows what is installed — the Medias page appears with its plugins, the
media manager only with its service. Held here, offline (throwaway database, test client):
  1. service absent and its templates unreachable → /medias 200, no manager tab, no manager view;
     the workspace 200, no media manager holder, the widget not offered;
  2. service present → the tab, the view, the holder and the widget are there.

    ./venv/bin/python tests/verif_medias_sans_media_manager.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_medias_mm_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

import main as _main                                                          # noqa: E402
from jinja2 import TemplateNotFound                                           # noqa: E402
from app import core_plugins                                                  # noqa: E402
from app.auth import hash_password                                            # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


uid = database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
database.db_upsert_container_docker(701, "essai", None, "bobi-701")
PID = database.db_save_project("Atelier", [701])
cli = _main.app.test_client()
with cli.session_transaction() as sess:
    sess["user_id"] = uid

env = _main.app.jinja_env
vrai_loader, vrai_is_service = env.loader, core_plugins.is_service


class SansService(type(vrai_loader)):
    """The real loader, except that the service's templates are not there."""
    def get_source(self, environment, template):
        if template.startswith("media_manager/"):
            raise TemplateNotFound(template)
        return super().get_source(environment, template)


print("\n1. service absent")
env.loader = SansService(_main.app)
env.cache.clear() if env.cache is not None else None
core_plugins.is_service = lambda sid: False if sid == "media_manager" else vrai_is_service(sid)
try:
    _main.app.testing = False                     # a 500 is a response here, not an exception
    r = cli.get("/medias")
    page = r.get_data(as_text=True)
    verifie(r.status_code == 200, "/medias answers 200", r.status_code)
    verifie('id="medias-mode-manager"' not in page, "no manager tab")
    verifie('id="medias-mgr-view"' not in page, "no manager view")
    r = cli.get("/workspace/%d" % PID)
    page = r.get_data(as_text=True)
    verifie(r.status_code == 200, "the workspace answers 200", r.status_code)
    verifie('id="wsp-mm-holder"' not in page, "no media manager holder in the workspace")
    verifie("const HAS_MM = false;" in page, "the media manager widget is not offered")
finally:
    env.loader, core_plugins.is_service = vrai_loader, vrai_is_service
    env.cache.clear() if env.cache is not None else None

print("\n2. service present")
if core_plugins.is_service("media_manager"):
    page = cli.get("/medias").get_data(as_text=True)
    verifie('id="medias-mode-manager"' in page and 'id="medias-mgr-view"' in page, "tab and view are there")
    page = cli.get("/workspace/%d" % PID).get_data(as_text=True)
    verifie('id="wsp-mm-holder"' in page and "const HAS_MM = true;" in page, "workspace: holder and widget are there")
else:
    print("  – media_manager not checked out here: skipped")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
