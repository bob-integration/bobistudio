#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Password profile « aucune » and the named refusal (2026-11-27), offline on a throwaway database.
  1. « aucune »: any non-empty password passes, the three always-on checks included; empty is refused;
     the other profiles keep refusing a password that reuses the username;
  2. POST /api/users: a refused password says WHICH rule it broke, not only « trop faible »;
  3. the profile is offered in Réglages → Sécurité and on the first-run screen.

    ./venv/bin/python tests/verif_motdepasse_aucune.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="verif_mdp_aucune_")
from app import config, database                                              # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "t.db")
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import auth, settings                                                 # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{str(detail)[:300]}]"))
    if not cond:
        ko += 1


print("1. the « aucune » profile")
ex = auth.pwd_exigences("aucune")
for pwd in ("a", "azerty", "jour2campagne", "1234", "aaaa"):
    f = auth.valider_motdepasse(pwd, "jour2campagne", exigences=ex)
    verifie(f == [], "★ « %s » accepted" % pwd, f)
verifie(auth.valider_motdepasse("", "x", exigences=ex) == ["court"], "an empty password is still refused")
for prof in ("souple", "standard", "stricte"):
    f = auth.valider_motdepasse("jour2campagne", "jour2campagne", exigences=auth.pwd_exigences(prof))
    verifie("identite" in f, "%s: reusing the username stays refused" % prof, f)
settings.set("pwd_profil", "aucune")
verifie(auth.pwd_profil() == "aucune", "the setting is taken, not dropped as unknown")

print("\n2. the refusal names the rule")
database.db_create_user("chef", auth.hash_password("Banc-Motdepasse-2026!"), "admin")
import main as app_main                                                        # noqa: E402
app = app_main.app
app.config["TESTING"] = True
cli = app.test_client()
with database.get_db() as db:
    u = db.execute("SELECT id, username FROM users WHERE username='chef'").fetchone()
with cli.session_transaction() as s:
    s["user_id"], s["username"] = u["id"], u["username"]
settings.set("pwd_profil", "souple")
r = cli.post("/api/users", json={"username": "jour2campagne", "password": "jour2campagne", "role": "viewer"})
j = r.get_json() or {}
verifie(r.status_code == 400 and j.get("regles") == ["identite"], "refused for the identity rule", (r.status_code, j))
verifie("identifiant" in (j.get("error") or "") or "username" in (j.get("error") or ""),
        "★ the message names the rule", j.get("error"))
settings.set("pwd_profil", "aucune")
r = cli.post("/api/users", json={"username": "jour2campagne", "password": "jour2campagne", "role": "viewer"})
verifie(r.status_code == 200, "★ accepted under « aucune »", (r.status_code, r.get_json()))

print("\n3. the page")
for tpl, needle in (("templates/settings.html", '<option value="aucune">'),
                    ("templates/setup.html", "'aucune', 'souple'")):
    verifie(needle in open(os.path.join(ROOT, tpl), encoding="utf-8").read(), "offered in " + tpl)

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
