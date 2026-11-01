#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : colonnes de la bibliothèque des médias — « Créé le » et ORDRE des colonnes (2026-10-29).

  1. sonde (script_templates/media_scan.py, VRAI ffprobe sur un fichier fabriqué) : la date de
     création inscrite dans le fichier est relevée ; la clé est posée même sans date ;
  2. base (app/media_registry.py, base JETABLE, analyse simulée) : `file_created` = date inscrite
     (heure locale), sinon la PLUS ANCIENNE date de fichier de l'original ; une date inscrite n'est
     jamais remplacée par une date de fichier ; une copie de version ne compte pas ; les fiches
     techniques d'avant (sans `created`) sont réanalysées ; champ interrogeable (« après ») ;
  3. interface (fonctions pures de static/js/media_library.js, node) : moveField — un cran, bornes,
     « avant telle colonne », en dernier, clé absente, liste par défaut comme départ ;
  4. câblage (source) : colonne et champ de carte proposés, bouton « Colonnes… » en vue liste,
     fenêtre ↑ ↓, glisser d'un en-tête qui n'est pas un tri ; libellés FR + EN.

    ./venv/bin/python tests/verif_media_colonnes.py
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="verif_ml_cols_")
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()
from app import media_registry as mr                                    # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")

# ── 1. Sonde ─────────────────────────────────────────────────────────────────────────────────
print("1. Sonde : date de création inscrite dans le fichier")
spec = importlib.util.spec_from_file_location("media_scan", os.path.join(ROOT, "script_templates", "media_scan.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("ffmpeg absent : sonde sautée")
else:
    with_d, without = os.path.join(TMP, "avec.mp4"), os.path.join(TMP, "sans.mp4")
    base = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x36:d=0.2", "-c:v", "libx264", "-y"]
    subprocess.run(base + ["-metadata", "creation_time=2026-01-02T03:04:05.000000Z", with_d], check=True)
    subprocess.run(base + ["-map_metadata", "-1", "-fflags", "+bitexact", without], check=True)
    t1, t2 = scan.probe(with_d), scan.probe(without)
    check(str((t1 or {}).get("created") or "").startswith("2026-01-02T03:04:05"), "date relevée (%s)" % (t1 or {}).get("created"))
    check(t2 is not None and "created" in t2, "sans date inscrite : la clé est posée quand même (%s)" % (t2 or {}).get("created"))

# ── 2. Base ──────────────────────────────────────────────────────────────────────────────────
print("2. Base : file_created")
check(mr._iso_local("2026-01-02T03:04:05.000000Z") ==
      time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1767323045)), "UTC « …Z » → heure locale")
check(mr._iso_local("2026-03-04T10:00:00") == "2026-03-04T10:00:00", "sans fuseau : gardée telle quelle")
check(mr._iso_local("n'importe quoi") is None and mr._iso_local("1970-01-01T00:00:00Z") is None and mr._iso_local(None) is None,
      "illisible, époque zéro (encodeur sans horloge), vide → aucune")

FILES, KNOWN = {}, []


def fake_run(node, req, timeout=0):
    if req["mode"] == "scan":
        KNOWN.append(req["known"])
        return {"files": [dict(f) for f in FILES.values()], "probed": 0}
    return {"stamped": {}, "thumbs": {}}


mr._run_on_node = fake_run
mr._node = lambda nid: {"id": int(nid), "name": "n1", "media_image": "img"}
TECH = {"duration": 1.0, "video": {"width": 64}}
T0 = 1767323045                                            # 2026-01-02 03:04:05 UTC
FILES["a.mp4"] = {"path": "a.mp4", "size": 10, "mtime": T0 + 999, "fp": "fa", "tech": dict(TECH, created="2026-01-02T03:04:05Z")}
FILES["b.mp4"] = {"path": "b.mp4", "size": 11, "mtime": T0 + 500, "fp": "fb", "tech": dict(TECH, created=None)}
mr.scan_node(1)
ida, idb = mr.import_files(1, ["a.mp4", "b.mp4"])
A, B = mr.get_asset(ida), mr.get_asset(idb)
loc = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
check(A["file_created"] == loc(T0) and A["file_created_src"] == "meta", "date inscrite → retenue, source « meta » (%s)" % A["file_created"])
check(B["file_created"] == loc(T0 + 500) and B["file_created_src"] == "file", "sans date inscrite → date du fichier (%s)" % B["file_created"])
# Une copie plus ANCIENNE (même empreinte, autre chemin) : la date du fichier recule ; une plus récente ne compte pas.
FILES["b-old.mp4"] = dict(FILES["b.mp4"], path="b-old.mp4", mtime=T0 + 100)
FILES["b-new.mp4"] = dict(FILES["b.mp4"], path="b-new.mp4", mtime=T0 + 900)
FILES["a-copy.mp4"] = dict(FILES["a.mp4"], path="a-copy.mp4", mtime=T0 - 5000, tech=dict(TECH, created=None))
mr.scan_node(1)
A, B = mr.get_asset(ida), mr.get_asset(idb)
check(B["file_created"] == loc(T0 + 100), "la plus ANCIENNE date de fichier l'emporte (%s)" % B["file_created"])
check(A["file_created"] == loc(T0) and A["file_created_src"] == "meta",
      "une date inscrite n'est jamais remplacée par une date de fichier, même plus ancienne")
# Version (transcodage) d'un média : sa date ne compte pas.
c = mr.db()
c.execute("UPDATE media_files SET version='h264' WHERE path='b-old.mp4'")
c.execute("UPDATE media_assets SET file_created=NULL, file_created_src=NULL WHERE id=?", (idb,))
c.commit()
FILES["b-old.mp4"]["version"] = "h264"
mr.scan_node(1)
check(mr.get_asset(idb)["file_created"] == loc(T0 + 500), "une copie de VERSION ne compte pas (%s)" % mr.get_asset(idb)["file_created"])
# Fiche technique d'avant le relevé (sans `created`) : redemandée.
c.execute("UPDATE media_files SET tech_json=? WHERE path='a.mp4'", (json.dumps(TECH),))
c.commit()
mr.scan_node(1)
k = KNOWN[-1]
check(k["a.mp4"][3] is False and k["b.mp4"][3] is True, "fiche sans `created` réanalysée, les autres non (%s / %s)"
      % (k["a.mp4"][3], k["b.mp4"][3]))
after = lambda d: sorted(a["id"] for a in mr.search({"items": [{"field": "file_created", "op": "after", "value": d}]}))
check("file_created" in {f["key"] for f in mr.search_fields()}, "champ « Créé le » proposé à la recherche")
check(after("2025-12-31") == sorted([ida, idb]) and after("2026-01-04") == [],
      "recherche « Créé le après » : tient compte de la date (%s / %s)" % (len(after("2025-12-31")), len(after("2026-01-04"))))

# ── 3. Interface : moveField ─────────────────────────────────────────────────────────────────
print("3. Interface : ordre des colonnes")
JS = os.path.join(ROOT, "static", "js", "media_library.js")
if not shutil.which("node"):
    print("node absent : section sautée")
else:
    prog = r"""
const M = require(process.argv[2]); const D = ['duration','format','audio','copies'];
console.log(JSON.stringify({
  up: M.moveField(['a','b','c'], 'b', -1), down: M.moveField(['a','b','c'], 'b', 1),
  top: M.moveField(['a','b','c'], 'a', -1), bottom: M.moveField(['a','b','c'], 'c', 1),
  before: M.moveField(['a','b','c','d'], 'd', 'b'), before_r: M.moveField(['a','b','c','d'], 'a', 'c'),
  last: M.moveField(['a','b','c'], 'a', null), self: M.moveField(['a','b','c'], 'b', 'b'),
  absent: M.moveField(['a','b'], 'z', 1), def: M.moveField(null, 'copies', 'duration', D),
  same: (() => { const l = ['a','b']; M.moveField(l, 'a', 1); return l; })()}));
"""
    f = os.path.join(TMP, "p.js")
    open(f, "w").write(prog)
    r = subprocess.run(["node", f, JS], capture_output=True, text=True, timeout=30)
    o = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    check(r.returncode == 0, "fonctions pures chargées (%s)" % r.stderr.strip()[:200])
    check(o.get("up") == ["b", "a", "c"] and o.get("down") == ["a", "c", "b"], "un cran à gauche / à droite")
    check(o.get("top") == ["a", "b", "c"] and o.get("bottom") == ["a", "b", "c"], "bornée aux extrémités")
    check(o.get("before") == ["a", "d", "b", "c"] and o.get("before_r") == ["b", "a", "c", "d"],
          "glisser : posée AVANT la colonne visée (vers la gauche comme vers la droite)")
    check(o.get("last") == ["b", "c", "a"] and o.get("self") == ["a", "b", "c"], "au-delà de la dernière : en dernier ; sur elle-même : rien")
    check(o.get("absent") == ["a", "b"], "clé absente : rien ne bouge")
    check(o.get("def") == ["copies", "duration", "format", "audio"], "départ = colonnes par défaut")
    check(o.get("same") == ["a", "b"], "la liste reçue n'est pas modifiée en place")

# ── 4. Câblage ───────────────────────────────────────────────────────────────────────────────
print("4. Câblage")
src = open(JS, encoding="utf-8").read()
check("'copies', 'created_at', 'file_created', 'title'" in src and "'origin', 'created_at', 'file_created', 'size'" in src,
      "« Créé le » proposée en colonne et sur les cartes")
check("'created_at','file_created'" in src, "… et dans l'éditeur de recherche")
check("$('.ml-cols-btn').hidden = !(mode === 'table' && st.tab !== 'net')" in src, "bouton « Colonnes… » en vue liste seulement")
check(src.count("moveField(colsOf()") == 2, "fenêtre (↑ ↓) ET glisser d'un en-tête passent par moveField")
check("_rsAt = Date.now(); th.classList.remove('ml-col-drag')" in src, "un glisser d'en-tête n'est pas pris pour un tri")
check("label:L('cols_organize'" in src, "clic droit sur l'en-tête : « Organiser les colonnes… »")
keys = set(re.findall(r"L\('((?:col_created|created_meta|created_file|cols_[a-z]+))'", src)) | {"sf.file_created"}
fr = json.load(open(os.path.join(ROOT, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(ROOT, "i18n", "en.json"), encoding="utf-8"))
miss = sorted(k for k in keys if "medialib." + k not in fr or "medialib." + k not in en)
check(len(keys) >= 10 and not miss, "%d libellés, FR + EN%s" % (len(keys), (" — manquants : %s" % miss) if miss else ""))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
