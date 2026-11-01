#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde : le logo de marque n'est jamais PERSISTÉ, seulement ENVOYÉ (24/10).

`_resolve_brand_settings` mettait le logo (≈300 Ko de base64) dans les params, qui finissaient
dans `containers.deploy_config`. Sept mires en portaient 2,3 Mo : chaque `db_get_containers` les
relisait, et une dizaine de boucles de fond l'appellent à chaque tour — la moitié du CPU de
l'orchestrateur, et la page Câbles attendait 5 à 10 s sa topologie.

Vérifie, sans réseau ni base :
  1. les params persistés portent l'extension du logo, pas son contenu ;
  2. un logo hérité d'un ancien snapshot de projet est vidé au passage ;
  3. `_gras` (params ENVOYÉS) embarque le logo, à l'octet près, sans muter son entrée ;
  4. sans logo téléversé : ni extension ni contenu, et `_gras` n'invente rien ;
  5. `init_db` purge le logo des conteneurs et des snapshots existants (base jetable, processus à
     part : la connexion par thread garderait sinon la base de prod).
"""
import base64
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import deploy, settings  # noqa: E402

fails = []


def check(cond, what, extra=None):
    print(("OK   " if cond else "FAIL ") + what + ("" if cond or extra is None else "  → %.300r" % (extra,)))
    if not cond:
        fails.append(what)


settings.get = lambda k, *a, **kw: ""
tmp = tempfile.mkdtemp()
deploy._STATIC_UPLOADS = tmp
LOGO = os.urandom(5000)
with open(os.path.join(tmp, "brand-logo.png"), "wb") as f:
    f.write(LOGO)

# 1.
lean = deploy._resolve_brand_settings({"pattern": "bars"})
check(lean.get("brand_logo_b64") == "" and lean.get("brand_logo_ext") == "png",
      "1. persisté : extension seule, aucun base64", {k: str(v)[:20] for k, v in lean.items()})

# 2.
old = deploy._resolve_brand_settings({"brand_logo_b64": "QUFB" * 1000, "brand_logo_ext": "jpg"})
check(old.get("brand_logo_b64") == "" and old.get("brand_logo_ext") == "png",
      "2. logo d'un ancien snapshot vidé")

# 3.
sent = deploy._gras(lean)
check(base64.b64decode(sent.get("brand_logo_b64") or "") == LOGO and sent.get("brand_logo_ext") == "png",
      "3. envoyé : le logo embarqué à l'octet près")
check(lean.get("brand_logo_b64") == "", "3b. `_gras` ne mute pas les params persistés")
check("brand_logo_b64" not in deploy._gras({"pattern": "bars"}),
      "3c. un plugin sans marque ne reçoit pas de logo")

# 4.
os.remove(os.path.join(tmp, "brand-logo.png"))
none = deploy._resolve_brand_settings({})
check(none.get("brand_logo_ext") == "" and not deploy._gras(none).get("brand_logo_b64"),
      "4. sans logo téléversé : rien d'embarqué")

# 5.
MIGR = r"""
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from app import database
database.DB_PATH = sys.argv[2]
database.init_db()
db = sqlite3.connect(sys.argv[2])
dc = {"type": "avsync", "params": {"brand_logo_b64": "QUFB" * 9000, "brand_logo_ext": "png", "pattern": "bars"}}
db.execute("INSERT INTO containers (vmid, hostname, deploy_config) VALUES (900, 'mire', ?)", (json.dumps(dc),))
db.execute("INSERT INTO projects (name, snapshot) VALUES ('P', ?)", (json.dumps([{"deploy_config": dc}]),))
db.commit(); db.close()
database.init_db()
db = sqlite3.connect(sys.argv[2])
print(json.dumps([json.loads(db.execute("SELECT deploy_config FROM containers WHERE vmid=900").fetchone()[0]),
                  json.loads(db.execute("SELECT snapshot FROM projects WHERE name='P'").fetchone()[0])]))
"""
r = subprocess.run([sys.executable, "-c", MIGR, RACINE, os.path.join(tmp, "migr.db")],
                   capture_output=True, text=True, timeout=120)
try:
    cdc, snap = json.loads(r.stdout.strip().splitlines()[-1])
except Exception:
    cdc, snap = {}, [{}]
cp, sp = cdc.get("params") or {}, (snap[0].get("deploy_config") or {}).get("params") or {}
check(cp.get("brand_logo_b64") == "" and cp.get("brand_logo_ext") == "png" and cp.get("pattern") == "bars",
      "5. init_db : logo purgé du deploy_config, le reste intact", cp or r.stderr[-400:])
check(sp.get("brand_logo_b64") == "" and sp.get("pattern") == "bars",
      "5b. init_db : logo purgé du snapshot de projet", sp)

if fails:
    print("\nÉCHEC — %d vérification(s)" % len(fails))
    sys.exit(1)
print("\nOK — logo de marque maigre en base, gras à l'envoi")
