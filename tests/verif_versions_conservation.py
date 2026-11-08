#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Versions automatiques de projet : délai et règles de conservation réglables (2026-11-07).
Avant : 10 s et 30 versions, écrits en dur. Demande : « en conserver 5 sur les dernières 24 h,
puis 1 par jour, puis 1 par semaine, etc. ».

Vérifié hors ligne (base jetable, vraies routes) :
  1. sans règle, le défaut fait exactement ce qui existait (les 30 plus récentes) ;
  2. l'exemple de la demande, sur des âges posés à la main : 5 sur 24 h, 1 par jour, 1 par
     semaine, 1 par mois au-delà ;
  3. sans tranche « au-delà », ce qui dépasse la dernière tranche part ;
  4. une date illisible n'est jamais supprimée ;
  5. les règles incohérentes sont refusées (tranches qui ne croissent pas, « au-delà » pas en
     dernier, mode inconnu, délai hors bornes) ;
  6. en base : les versions NOMMÉES ne sont jamais touchées, l'aperçu annonce exactement ce que
     l'enregistrement supprime ;
  7. routes : lecture, aperçu, enregistrement (qui applique aussitôt), droits ;
  8. le délai réglé est celui du re-snapshot automatique ;
  9. Réglages → Système → Projets porte l'éditeur, et son JS se lit.

⚠ Garde MUTÉE à l'écriture.
"""
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="versions_conservation_db_")
_DBF = os.path.join(_TMP, "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


from app import project_versions as pv            # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, 0)             # un samedi


def at(hours):
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


def vers(ages):
    return [{"id": i + 1, "created_at": at(h)} for i, h in enumerate(ages)]


def kept_ages(ages, policy):
    vs = vers(ages)
    k = pv.kept_ids(vs, policy, now=NOW)
    return sorted(h for h, v in zip(ages, vs) if v["id"] in k)


print("1. Défaut = l'existant")
ages = list(range(1, 41))
ok(kept_ages(ages, pv.DEFAULT_POLICY) == list(range(1, 31)), "les 30 plus récentes sur 40", kept_ages(ages, pv.DEFAULT_POLICY))

print("2. L'exemple de la demande")
EX = {"delay_s": 10, "max": 0, "tiers": [{"upto_h": 24, "keep": "count", "n": 5}, {"upto_h": 168, "keep": "daily"},
                                         {"upto_h": 1344, "keep": "weekly"}, {"upto_h": None, "keep": "monthly"}]}
# 1-7 h : 7 dans les 24 h → les 5 plus récentes. 30 h (06/11), 40 h et 50 h (tous deux le 05/11) → 30 h et 40 h.
# 200 h et 210 h : même semaine ISO (44) → 200 h. 2000 h et 2010 h : même mois (août) → 2000 h.
ages = [1, 2, 3, 4, 5, 6, 7, 30, 40, 50, 200, 210, 2000, 2010]
ok(kept_ages(ages, EX) == [1, 2, 3, 4, 5, 30, 40, 200, 2000], "5 · 1/jour · 1/semaine · 1/mois", kept_ages(ages, EX))
ok(kept_ages(ages, dict(EX, max=4)) == [1, 2, 3, 4], "le maximum s'applique après les tranches, aux plus récentes",
   kept_ages(ages, dict(EX, max=4)))

print("3. Sans « au-delà »")
ok(kept_ages(ages, {"max": 0, "tiers": [{"upto_h": 24, "keep": "count", "n": 2}]}) == [1, 2], "au-delà de 24 h : supprimées")
ok(kept_ages([1, 2, 30], {"max": 0, "tiers": [{"upto_h": 24, "keep": "none"}, {"upto_h": None, "keep": "all"}]}) == [30],
   "« aucune » vide la tranche, « toutes » garde tout")
# 0,2 h (11 h 48) et 0,5 h (11 h 30) : même heure → la plus récente ; 3 h (9 h) : une autre heure.
ok(kept_ages([0.2, 0.5, 3], {"max": 0, "tiers": [{"upto_h": None, "keep": "hourly"}]}) == [0.2, 3],
   "une par heure : la plus récente de chaque heure")

print("4. Date illisible")
k = pv.kept_ids([{"id": 1, "created_at": "n'importe quoi"}, {"id": 2, "created_at": at(500)}],
                {"max": 0, "tiers": [{"upto_h": 24, "keep": "all"}]}, now=NOW)
ok(k == {1}, "gardée (on ne supprime pas ce qu'on ne sait pas placer)", k)

print("5. Règles refusées")
for pol, why in (({"tiers": [{"upto_h": 48, "keep": "all"}, {"upto_h": 24, "keep": "all"}]}, "tranches qui ne croissent pas"),
                 ({"tiers": [{"upto_h": None, "keep": "all"}, {"upto_h": 24, "keep": "all"}]}, "« au-delà » pas en dernier"),
                 ({"tiers": [{"upto_h": 24, "keep": "souvent"}]}, "mode inconnu"),
                 ({"tiers": [{"upto_h": 24, "keep": "count", "n": 0}]}, "nombre nul"),
                 ({"delay_s": 1}, "délai trop court"), ({"max": -1}, "maximum négatif")):
    try:
        pv.validate(pol)
        ok(False, why, "accepté")
    except ValueError:
        ok(True, why)

print("6. En base")
for v, hn in ((401, "a"), (402, "b")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, "mixer", {"hostname": hn})
PID = database.db_save_project("Atelier", [401, 402])
for i in range(35):
    database.db_add_project_version(PID, [], label=None)
database.db_add_project_version(PID, [], label="Avant le direct")
vs = database.db_project_versions(PID)
ok(sum(1 for v in vs if not v["label"]) == 30 and any(v["label"] == "Avant le direct" for v in vs),
   "défaut : 30 automatiques gardées, la nommée aussi", len(vs))
# Vieillir les versions : une par jour sur 30 jours.
with database.get_db() as db:
    ids = [r[0] for r in db.execute("SELECT id FROM project_versions WHERE project_id=? AND label IS NULL ORDER BY id DESC", (PID,))]
    for n, vid in enumerate(ids):
        db.execute("UPDATE project_versions SET created_at=? WHERE id=?",
                   ((datetime.now() - timedelta(days=n, hours=1)).isoformat(timespec="seconds"), vid))
    db.execute("UPDATE project_versions SET created_at=? WHERE label IS NOT NULL",
               ((datetime.now() - timedelta(days=400)).isoformat(timespec="seconds"),))
    db.commit()
RULE = {"delay_s": 25, "max": 0, "tiers": [{"upto_h": 72, "keep": "all"}]}
prev = pv.preview(RULE)

print("7. Routes")
from app.auth import hash_password                # noqa: E402
uid = database.db_create_user("admin_t", hash_password("x"), "admin")
vid_ = database.db_create_user("lecteur_t", hash_password("x"), "viewer")
import main as app_main                           # noqa: E402
app = app_main.app
app.config["TESTING"] = True
cli_v = app.test_client()
with cli_v.session_transaction() as s:
    s["user_id"] = vid_
    s["username"] = "lecteur_t"
r = cli_v.post("/api/project_versions/policy", json={"policy": RULE})
ok(r.status_code == 403, "un lecteur ne peut pas enregistrer", r.status_code)
cli = app.test_client()
with cli.session_transaction() as s:
    s["user_id"] = uid
    s["username"] = "admin_t"
r = cli.get("/api/project_versions/policy")
ok(r.status_code == 200 and r.get_json()["policy"] == pv.DEFAULT_POLICY, "lecture : le défaut", r.get_json())
r = cli.post("/api/project_versions/preview", json={"policy": {"tiers": [{"upto_h": 24, "keep": "nope"}]}})
ok(r.status_code == 400, "aperçu d'une règle invalide : 400", r.status_code)
r = cli.post("/api/project_versions/preview", json={"policy": RULE})
ok(r.status_code == 200 and r.get_json()["delete"] == prev["delete"] > 0, "aperçu par la route", r.get_json())
r = cli.post("/api/project_versions/policy", json={"policy": RULE})
vs = database.db_project_versions(PID)
auto = [v for v in vs if not v["label"]]
ok(r.status_code == 200 and r.get_json()["deleted"] == prev["delete"] and len(auto) == 3,
   "enregistrer applique aussitôt : 3 jours gardés, autant que l'aperçu annonçait", (r.get_json(), len(auto)))
ok(any(v["label"] == "Avant le direct" for v in vs), "la version nommée (400 jours) est toujours là")
ok(pv.current() == pv.validate(RULE), "la règle est enregistrée", pv.current())

print("8. Délai")
from app import projects                          # noqa: E402
SEEN = []


class _Timer:                                     # espion : on lit l'attente, on ne lance rien
    daemon = False

    def __init__(self, interval, fn, args=()):
        SEEN.append(interval)

    def start(self):
        pass

    def cancel(self):
        pass


database.db_set_project(401, PID)
from app import auth as _auth                     # noqa: E402
_auth._vmid_project_cache.clear()
_auth._snap_map_cache["ts"] = 0.0
_real = projects.threading.Timer
projects.threading.Timer = _Timer
try:
    projects.notify_container_changed(401)
finally:
    projects.threading.Timer = _real
ok(SEEN and all(x == 25 for x in SEEN), "une modification de conteneur attend le délai réglé", SEEN)

print("9. Réglages")
html = cli.get("/settings").get_data(as_text=True)
_tab = html[html.find('id="set-tab-projets"'):]
_tab = _tab[:_tab.find('<div id="set-tab-', 1)]          # jusqu'à l'onglet suivant
ok(html.count('id="vpol-editor"') == 1 and 'id="vpol-editor"' in _tab and "js/version_policy.js" in _tab,
   "l'éditeur est dans l'onglet Réglages → Projets", len(_tab))
ok(re.search(r"\{id: 'systeme',\s*tabs: \[[^\]]*'projets'", html) is not None and 'id="set-tab-btn-projets"' in html,
   "l'onglet Projets est dans le groupe Système")
chk = subprocess.run(["node", "--check", os.path.join(RACINE, "static", "js", "version_policy.js")],
                     capture_output=True, text=True)
ok(chk.returncode == 0, "le JS de l'éditeur se lit", chk.stderr[:300])
keys = set(re.findall(r"T\('(settings\.vpol\.[a-z_]+)", open(os.path.join(RACINE, "static", "js", "version_policy.js"),
                                                             encoding="utf-8").read()))
import json                                       # noqa: E402
fr = json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
dyn = {"settings.vpol.unit_", "settings.vpol.mode_", "settings.vpol.age_", "settings.vpol.phr_"}
manque = sorted(k for k in keys if k not in fr and k not in dyn)
ok(not manque, "toutes les clés de l'éditeur existent", manque)

print("10. Ce que l'éditeur annonce")
js = open(os.path.join(RACINE, "static", "js", "version_policy.js"), encoding="utf-8").read()
harness = "const window = {};\n" + js + """
const V = window.VersionPolicy, ex = V.EXAMPLE.tiers, sp = V._spans(ex);
console.log(JSON.stringify({spans: sp, most: ex.map((t, i) => { const m = V._most(t, sp[i]); return m === Infinity ? 'inf' : m; })}));
"""
f = os.path.join(_TMP, "vpol.js")
open(f, "w", encoding="utf-8").write(harness)
run = subprocess.run(["node", f], capture_output=True, text=True)
try:
    res = json.loads(run.stdout.strip().splitlines()[-1])
except Exception:
    res = {"error": run.stderr[:300]}
# 5 sur 24 h ; une par jour de 24 h à 7 j = 6 ; une par semaine de 7 j à 8 sem. = 7 ; une par mois au-delà : sans limite.
ok(res.get("spans") == [[0, 24], [24, 168], [168, 1344], [1344, None]] and res.get("most") == [5, 6, 7, "inf"],
   "tranches et « au plus » de l'exemple : 5 · 6 · 7 · sans limite", res)

print()
if ECHECS:
    print("ÉCHEC : %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("OK")
