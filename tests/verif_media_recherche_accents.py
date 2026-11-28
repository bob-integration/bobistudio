#!/usr/bin/env python3
"""Media base search ignores accents and case (2026-11-22).

A library of portraits for a political show is searched by people's names, typed fast and often
without accents. SQLite's LOWER only lowers ASCII: « melenchon » did not find « Mélenchon », and
« élisabeth » did not even find « Élisabeth ». Guard: free text, `contains` / `not_contains` and
`eq` on a text field all go through fold(); a value with accents still finds itself; the
library's own filters (network share, extra tabs) follow the same rule."""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_accents_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
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
for aid, name, meta in (("1", "Élisabeth_Borne.jpg", {"title": "Élisabeth Borne", "keywords": "Gouvernement, Matignon"}),
                        ("2", "portrait.jpg", {"title": "Jean-Luc Mélenchon", "keywords": "Député"}),
                        ("3", "IMG_0042.JPG", {"description": "Meeting à Besançon"}),
                        ("4", "plateau.png", {"title": "Plateau"})):
    c.execute("INSERT INTO media_assets(id, name, meta_json, tech_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              (aid, name, json.dumps(meta, ensure_ascii=False), json.dumps({"video": {"codec": "png"}}),
               "2026-11-22T10:00:0" + aid, "2026-11-22T10:00:00"))
c.commit()


def ids(q):
    return sorted(a["id"] for a in mr.search(q))


print("fold()")
check(mr.fold("Élisabeth") == "elisabeth" and mr.fold("MÉLENCHON") == "melenchon" and mr.fold(None) == "",
      "accents dropped, case folded, None → ''")

print("free text")
check(ids({"q": "melenchon"}) == ["2"], "« melenchon » finds « Mélenchon »")
check(ids({"q": "élisabeth"}) == ["1"] and ids({"q": "ELISABETH"}) == ["1"], "« élisabeth » and « ELISABETH » find « Élisabeth »")
check(ids({"q": "Mélenchon"}) == ["2"], "a query with accents still finds itself")
check(ids({"q": "besancon"}) == ["3"], "description searched too, accents ignored")
check(ids({"q": "deput"}) == ["2"], "keywords searched, part of a word")
check(ids({"q": "img_0042"}) == ["3"], "file name, case ignored")

print("criteria")
check(ids({"q": "", "items": [{"field": "title", "op": "contains", "value": "elisa"}], "match": "all"}) == ["1"],
      "contains: accents ignored")
check(ids({"q": "", "items": [{"field": "title", "op": "not_contains", "value": "MELENCHON"}], "match": "all"}) == ["1", "3", "4"],
      "not_contains: accents and case ignored (a media without title is kept)")
check(ids({"q": "", "items": [{"field": "title", "op": "eq", "value": "elisabeth borne"}], "match": "all"}) == ["1"],
      "eq on a text field: accents and case ignored")
check(ids({"q": "", "items": [{"field": "keywords", "op": "contains", "value": "député"}], "match": "all"}) == ["2"],
      "keywords contains, with accents")

print("library filters")
js = open(os.path.join(RACINE, "static", "js", "media_library.js"), encoding="utf-8").read()
check("const foldTxt" in js and js.count("foldTxt(q)") >= 3 and ".toLowerCase().includes(q)" not in js,
      "share and extra-tab filters fold their text too")

print("\n%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
