#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des gestes de la BIBLIOTHÈQUE COMMUNE côté serveur (app/media_registry.py) — sans nœud :
# les commandes « sur le nœud » s'exécutent dans un dossier temporaire, avec un vrai shell (le
# renommage est donc éprouvé avec de vrais mv / sed, pas simulé).
#
# ★ CE QUI EST VÉRIFIÉ :
#   1. renommer le FICHIER d'une copie : fichier et sidecar renommés, <file> du sidecar suivi,
#      emplacement mis à jour en base, identité ET nom du média inchangés ; refus si la cible
#      existe (sur disque ou en base), nom invalide refusé ;
#   2. sidecar brut d'une copie lu tel quel (et None s'il n'y en a pas) ;
#   3. valeurs proposées par champ (recherche avancée) : distinctes, comptées, nœuds / projets
#      nommés, codecs audio éclatés par piste, options d'un champ liste ajoutées ;
#   4. bin parent : avec `children`, le contenu des sous-bins (manuels et intelligents) ;
#   5. copie de tout un bin : ce qui est déjà là n'est pas recopié, les erreurs sont nommées,
#      l'avancement est rapporté ; vers un nœud OU pour un conteneur (rapatriement) ;
#   6. nettoyage : les tables des anciens bins de la base PRINCIPALE sont supprimées une fois la
#      reprise faite, et la reprise reste sans erreur quand elles n'existent plus ;
#   7. recherche « copie sur ce partage ».
#
#   $ ./venv/bin/python tests/verif_media_bibliotheque.py
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_bib_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()
from app import media_registry as mr                                    # noqa: E402

mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
VOL = os.path.join(TMP, "vol")
os.makedirs(os.path.join(VOL, "d"))

ok = fail = 0


def check(cond, msg):
    global ok, fail
    if cond:
        ok += 1
        print("  ✓", msg)
    else:
        fail += 1
        print("  ✗", msg)


mr._node = lambda nid: {"id": int(nid), "name": "n%s" % nid, "media_image": "img", "host": "h", "media_mount": VOL}
_database.db_get_nodes = lambda: [{"id": 1, "name": "n1"}, {"id": 2, "name": "n2"}, {"id": 3, "name": "vide"}]
_database.db_get_projects = lambda: [{"id": 7, "name": "Régie"}, {"id": 8, "name": "Autre"}]
import app.node_driver as _nd                                           # noqa: E402


def local_exec(node, cmd, input_data=None, timeout=0):
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


_nd.host_exec = local_exec

c = mr.db()


def asset(aid, name, tech, meta=None):
    import json
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, tech_json, meta_json) VALUES (?,?,?,?,?,?,?)",
              (aid, name, "2026-10-11T10:00:00", "2026-10-11T10:00:00", "import", json.dumps(tech), json.dumps(meta or {})))


def copy(aid, node, path, share=0, state="present"):
    c.execute("INSERT INTO media_files(node_id, share_id, path, asset_id, state) VALUES (?,?,?,?,?)", (node, share, path, aid, state))


asset("A", "Interview", {"video": {"codec": "h264", "height": 1080, "fps": 25}, "audio": [{"codec": "aac"}, {"codec": "pcm_s24le"}]})
asset("B", "Plateau", {"video": {"codec": "h264", "height": 720, "fps": 50}, "audio": [{"codec": "aac"}]})
asset("C", "Habillage", {"video": {"codec": "prores", "height": 1080, "fps": 25}, "audio": []})
copy("A", 1, "d/interview.mxf")
copy("B", 1, "d/plateau.mov")
copy("B", 2, "d/plateau.mov")
copy("C", 0, "Archives/hab.mov", share=5)
c.execute("INSERT INTO project_media(project_id, asset_id) VALUES (7, 'A'), (7, 'B')")
c.commit()

print("1. renommer le fichier d'une copie")
with open(os.path.join(VOL, "d/interview.mxf"), "w") as f:
    f.write("x")
with open(os.path.join(VOL, "d/interview.mxf.xml"), "w") as f:
    f.write('<?xml version="1.0"?>\n<media id="A">\n  <file>interview.mxf</file>\n</media>\n')
new = mr.rename_copy("A", 1, "d/interview.mxf", "Interview maire [v2].mxf")
check(new == "d/Interview maire [v2].mxf" and os.path.exists(os.path.join(VOL, new))
      and not os.path.exists(os.path.join(VOL, "d/interview.mxf")), "fichier renommé sur le nœud")
sc = open(os.path.join(VOL, new + ".xml")).read()
check(not os.path.exists(os.path.join(VOL, "d/interview.mxf.xml")) and '<media id="A">' in sc
      and "<file>Interview maire [v2].mxf</file>" in sc, "sidecar suivi : renommé, identité intacte, <file> mis à jour")
a = mr.get_asset("A")
check([x["path"] for x in a["copies"]] == [new] and a["name"] == "Interview" and a["id"] == "A",
      "emplacement mis à jour, identité et nom du média inchangés")
with open(os.path.join(VOL, "d/occupe.mxf"), "w") as f:
    f.write("y")
for nom, raison in (("occupe.mxf", "fichier présent sur disque"), ("plateau.mov", "copie connue en base"),
                    ("../evasion.mxf", "nom avec /"), ("x.xml", "nom de sidecar"), ("", "nom vide")):
    try:
        mr.rename_copy("A", 1, new, nom)
        check(False, "refus : " + raison)
    except ValueError:
        check(os.path.exists(os.path.join(VOL, new)), "refus : " + raison + " (rien n'a bougé)")
try:
    mr.rename_copy("A", 2, new, "z.mxf")
    check(False, "copie inconnue sur ce nœud refusée")
except ValueError:
    check(True, "copie inconnue sur ce nœud refusée")
check(mr.rename_copy("A", 1, new, "interview.mxf") == "d/interview.mxf", "et remis à son nom")

print("2. sidecar brut")
x = mr.read_sidecar("A", 1, "d/interview.mxf")
check(x and '<media id="A">' in x and "<file>interview.mxf</file>" in x, "sidecar lu tel quel")
with open(os.path.join(VOL, "d/plateau.mov"), "w") as f:
    f.write("z")
check(mr.read_sidecar("B", 1, "d/plateau.mov") is None, "pas de sidecar : None")
try:
    mr.read_sidecar("B", 1, "d/interview.mxf")
    check(False, "sidecar d'une copie d'un AUTRE média refusé")
except ValueError:
    check(True, "sidecar d'une copie d'un AUTRE média refusé")

print("3. valeurs proposées")
v = {x["value"]: x["count"] for x in mr.search_values("vcodec")}
check(v == {"h264": 2, "prores": 1}, "codec vidéo : valeurs distinctes comptées")
check([x["value"] for x in mr.search_values("vcodec")][0] == "h264", "les plus fréquentes d'abord")
check({x["value"]: x["count"] for x in mr.search_values("audio_codec")} == {"aac": 2, "pcm_s24le": 1},
      "codec audio : éclaté piste par piste")
check({x["value"]: x["count"] for x in mr.search_values("height")} == {1080: 2, 720: 1}, "définition (nombre)")
nodes = {x["label"]: x["count"] for x in mr.search_values("on_node")}
check(nodes == {"n1": 2, "n2": 1, "vide": 0}, "nœuds nommés, avec et sans copie")
check({x["label"]: x["count"] for x in mr.search_values("project")} == {"Régie": 2, "Autre": 0}, "projets nommés")
check([x["value"] for x in mr.search_values("on_share")] == [5], "partages portant une copie")
check(mr.search_values("created_at") == [], "date : pas de liste")
mr.save_field("support", "Support", "enum", ["HDCAM", "XDCAM"])
import json                                                             # noqa: E402
c.execute("UPDATE media_assets SET meta_json=? WHERE id='A'", (json.dumps({"support": "XDCAM"}),))
c.commit()
check({x["value"]: x["count"] for x in mr.search_values("meta.support")} == {"XDCAM": 1, "HDCAM": 0},
      "champ liste : valeurs présentes + options déclarées")
try:
    mr.search_values("inconnu")
    check(False, "champ inconnu refusé")
except ValueError:
    check(True, "champ inconnu refusé")

print("4. bin parent et sous-bins")
p = mr.save_bin("Parent")
k1 = mr.save_bin("Enfant", parent_id=p)
k2 = mr.save_bin("Petit-enfant intelligent", parent_id=k1, query={"all": [{"field": "vcodec", "op": "eq", "value": "prores"}]})
mr.bin_add(p, ["A"])
mr.bin_add(k1, ["B", "A"])
check({a["id"] for a in mr.bin_assets(p)} == {"A"}, "sans option : le bin seul")
check({a["id"] for a in mr.bin_assets(p, children=True)} == {"A", "B", "C"}, "avec : sous-bins manuels et intelligents, sans doublon")
check({a["id"] for a in mr.bin_assets(p, extra={"q": "plat"}, children=True)} == {"B"}, "filtre appliqué au contenu")
check(mr.bin_subtree(p) == [p, k1, k2], "sous-arbre, parent d'abord")

print("5. copier tout un bin")
appels, avancement = [], []
mr.copy_asset = lambda aid, nid, rel=None, progress=None: appels.append(("node", aid, nid)) or "x"


def fetch(aid, vmid):
    if aid == "C":
        raise RuntimeError("aucune copie présente à rapatrier")
    appels.append(("vmid", aid, vmid))
    return "x"


mr.fetch_for_container = fetch
r = mr.copy_bin(p, node_id=2, progress=lambda d, t, n: avancement.append((d, t)))
check(sorted(r["copied"]) == ["A", "C"] and r["already"] == ["B"] and not r["errors"] and r["total"] == 3,
      "vers un nœud : déjà présent sauté, le reste copié")
check(avancement[0] == (0, 3) and avancement[-1] == (3, 3), "avancement rapporté")
appels.clear()
mr.container_view = lambda vmid: (1, "")
mr.link_container_project = lambda aid, vmid: None
r = mr.copy_bin(p, vmid=42)
check(r["copied"] == [], "pour un conteneur : présent sur son nœud = rien à faire")
check(sorted(r["already"]) == ["A", "B"] and list(r["errors"]) == ["Habillage"], "erreur nommée par le média, les autres continuent")
r = mr.copy_bin(k1, node_id=2, children=False)
check(r["total"] == 2, "sans les sous-bins : le bin seul")
try:
    mr.copy_bin(p)
    check(False, "cible requise")
except ValueError:
    check(True, "cible requise (nœud ou conteneur)")

print("6. nettoyage des anciens bins de la base principale")
g = _database.get_db()
g.execute("CREATE TABLE media_bins (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER, created_at TEXT)")
g.execute("CREATE TABLE media_bin_items (bin_id INTEGER, node_id INTEGER, path TEXT, kind TEXT, added_at TEXT)")
g.execute("INSERT INTO media_bins VALUES (10, 'Ancien', NULL, '')")
g.execute("INSERT INTO media_bin_items VALUES (10, 1, 'd/plateau.mov', 'clip', '')")
g.commit()
tables = lambda: {r[0] for r in g.execute("SELECT name FROM sqlite_master WHERE type='table'")}
res = mr.migrate_legacy_bins()
check(res == {"bins": 1, "items": 1, "skipped": 0}, "reprise faite")
check(not ({"media_bins", "media_bin_items"} & tables()), "anciennes tables supprimées de la base principale")
check({"media_bins", "media_bin_items"} <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")},
      "les bins de la base des médias, eux, restent")
check(mr.migrate_legacy_bins() is None and mr.drop_legacy_bins() is False, "une seule fois, sans erreur sans les tables")
# Base neuve, tables déjà absentes : la reprise passe et marque la suppression faite.
mr.MEDIA_DB_PATH = os.path.join(TMP, "neuve.db")
check(mr.migrate_legacy_bins() == {"bins": 0, "items": 0, "skipped": 0}, "reprise sans anciennes tables : rien, sans erreur")
check(mr.db().execute("SELECT 1 FROM media_meta WHERE key='legacy_bins_dropped'").fetchone() is not None, "et marquée faite")
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")

print("7. recherche « copie sur ce partage »")
check([a["id"] for a in mr.search({"all": [{"field": "on_share", "op": "eq", "value": 5}]})] == ["C"], "sur le partage 5")
check({a["id"] for a in mr.search({"all": [{"field": "on_share", "op": "ne", "value": 5}]})} == {"A", "B"}, "hors du partage 5")
check(any(f["key"] == "on_share" for f in mr.search_fields()), "champ proposé à l'éditeur")

print("\n%d contrôle(s) OK, %d en échec." % (ok, fail))
sys.exit(1 if fail else 0)
