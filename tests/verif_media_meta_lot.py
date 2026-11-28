#!/usr/bin/env python3
"""Metadata of a selection, in one go (2026-11-22).

A portrait library for a political show is tagged by batches: « Parti = X » on 30 photos, the
keyword « gouvernement » added to a whole selection, « ministre » removed after a reshuffle. Guard:
update_meta_bulk sets / clears fields, adds keywords without duplicates (accents and case ignored),
removes them the same way, leaves untouched media unchanged (not counted); keyword_values lists
keywords one by one; the route counts, refuses a viewer, and the library offers the dialog."""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_lot_")
import app.database as _database                                       # noqa: E402
from app import config                                                  # noqa: E402
config.DB_PATH = _database.DB_PATH = os.path.join(TMP, "principale.db")  # jamais la base de prod
_database._tls.__dict__.pop("conn", None)
_database.init_db()
from app import media_registry as mr                                    # noqa: E402

mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")

ok = fail = 0


def check(cond, msg):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("  ✓ " if cond else "  ✗ ") + msg)


c = mr.db()
for aid, meta in (("1", {"title": "Portrait A", "keywords": "Député, ministre"}),
                  ("2", {"keywords": "député"}),
                  ("3", {"description": "plateau"}),
                  ("4", {"keywords": "Gouvernement"})):
    c.execute("INSERT INTO media_assets(id, name, meta_json, tech_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              (aid, aid + ".jpg", json.dumps(meta, ensure_ascii=False), json.dumps({"video": {"codec": "png"}}),
               "2026-11-22T10:00:0" + aid, "2026-11-22T10:00:00"))
c.commit()
mr.save_field("parti", "Parti", "text")
mr.save_field("personne", "Personne", "text")


def meta(aid):
    return mr.get_asset(aid)["meta"]


print("update_meta_bulk")
n = mr.update_meta_bulk(["1", "2", "3"], {"parti": "Parti X", "inconnu": "zz", "keywords": "ignored"}, ["gouvernement", "Député"], [])
check(n == 3, "3 media changed (%d)" % n)
check(all(meta(a).get("parti") == "Parti X" for a in "123"), "custom field set on the whole selection")
check("inconnu" not in meta("1"), "an unknown field is ignored")
check(meta("1")["keywords"] == "Député, ministre, gouvernement", "keyword added once, existing « Député » kept (%s)" % meta("1")["keywords"])
check(meta("2")["keywords"] == "député, gouvernement", "« Député » not duplicated next to « député »")
check(meta("3")["keywords"] == "gouvernement, Député",
      "media without keywords gets both, in the order given (%s)" % meta("3")["keywords"])
check(meta("4").get("parti") is None, "media outside the selection untouched")

n = mr.update_meta_bulk(["1", "2", "4"], {}, [], ["MINISTRE", "deputé"])
check(meta("1")["keywords"] == "gouvernement", "removal ignores accents and case (%s)" % meta("1")["keywords"])
check(meta("2")["keywords"] == "gouvernement", "removal on every media of the selection")
check(n == 2, "media 4 had none of them: not counted (%d)" % n)

mr.update_meta_bulk(["2"], {}, [], ["gouvernement"])
check("keywords" not in meta("2"), "last keyword removed → the field disappears")
mr.update_meta_bulk(["1", "3"], {"parti": None}, [], [])
check("parti" not in meta("1") and "parti" not in meta("3"), "None clears a field on the selection")
check(mr.update_meta_bulk(["1"], {"title": "Portrait A"}, [], []) == 0, "same value → nothing written, not counted")

c.execute("INSERT INTO media_assets(id, name, meta_json, created_at, updated_at) VALUES ('5', '5.jpg', ?, '2026-11-22T10:00:05', '2026-11-22T10:00:00')",
          (json.dumps({"keywords": "Député, député ; DEPUTE"}, ensure_ascii=False),))
c.commit()
mr.update_meta_bulk(["5"], {}, ["x"], [])
check(meta("5")["keywords"] == "Député, x", "variants already stored are merged on the next edit (%s)" % meta("5")["keywords"])
c.execute("DELETE FROM media_assets WHERE id='5'"); c.commit()

print("keyword_values")
mr.update_meta_bulk(["1", "3", "4"], {}, ["Plateau"], [])
mr.update_meta_bulk(["2"], {}, ["plateau"], [])
vals = mr.keyword_values()
d = {v["value"]: v["count"] for v in vals}
check(d.get("Plateau") == 4 and "plateau" not in d, "one keyword per accent/case-insensitive spelling, most frequent spelling (%s)" % d)
check(vals[0]["value"] == "Plateau", "most used first")
check(any(v["value"].lower() == "gouvernement" and v["count"] == 3 for v in vals), "keywords counted one by one (%s)" % d)

print("route")
from app.auth import hash_password
_database.db_create_user("banc", hash_password("Banc-Motdepasse-2026!"), "admin")
_database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")
import main as app_main
app = app_main.app
app.config["TESTING"] = True
users = {r["username"]: r["id"] for r in _database.get_db().execute("SELECT id, username FROM users")}
cli_a, cli_l = app.test_client(), app.test_client()
for cli, nom in ((cli_a, "banc"), (cli_l, "lecteur")):
    with cli.session_transaction() as ss:
        ss["user_id"], ss["username"] = users[nom], nom
r = cli_a.post("/api/media/assets/meta_bulk", json={"asset_ids": ["1", "2"], "set": {"personne": "Marie Dupont"}, "kw_add": "portrait"})
dd = r.get_json() or {}
check(r.status_code == 200 and dd.get("changed") == 2 and dd.get("skipped") == 0, "route: changed 2, skipped 0 (%s)" % dd)
check(meta("2").get("personne") == "Marie Dupont" and "portrait" in meta("2").get("keywords", ""), "route: field and keyword written")
check(cli_a.post("/api/media/assets/meta_bulk", json={"asset_ids": []}).status_code == 400, "empty selection → 400")
check(cli_l.post("/api/media/assets/meta_bulk", json={"asset_ids": ["1"], "kw_add": ["x"]}).status_code == 403, "viewer refused")
kv = cli_a.get("/api/media/keywords").get_json() or {}
check(any(v["value"] == "Plateau" for v in kv.get("values", [])), "GET /api/media/keywords lists the keywords")
kv = cli_l.get("/api/media/keywords")
check(kv.status_code == 200 and (kv.get_json() or {}).get("values") == [],
      "a viewer with no project sees no keyword (bounded by what they can see)")

print("interface")
js = open(os.path.join(RACINE, "static", "js", "media_library.js"), encoding="utf-8").read()
check("async function openBulkMeta" in js and "'/api/media/assets/meta_bulk'" in js, "dialog posts to meta_bulk")
check('data-bulk="meta"' in js and "openBulkMeta(ids)" in js, "offered in the selection bar and the selection menu")
check("'/api/media/keywords'" in js and 'list="ml-bk-kwlist"' in js, "keywords suggested while typing")
check("closest('.ml-kw')" in js and "dispatchEvent(new Event('input'))" in js, "a keyword chip filters the list")
check("f.key === 'keywords' ? ' list=\"ml-bk-kwlist\"" in js, "the info sheet suggests keywords too")
for f in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", f + ".json"), encoding="utf-8"))
    check(all("medialib." + k in cat for k in ("bk_btn", "bk_title", "bk_kw_add", "bk_go", "bk_done", "bk_clear")), "i18n %s" % f)

print("\n%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
