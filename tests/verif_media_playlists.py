#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : les playlists sont des FICHES de la base des médias (décisions du 2026-11-07).

  1. créer : fiche de type « playlist », entrées normalisées, durée JOUÉE (entrée / sortie) ;
  2. dans le browser : la recherche par type les trouve, et seulement elles ;
  3. lire : nom et durée de chaque média, entrée dont le média a disparu signalée ;
  4. enregistrer : nouveau contenu, nouveau nom, durée recalculée ;
  5. exporter puis importer ailleurs : média retrouvé par identité, sinon par EMPREINTE, sinon
     gardé et compté introuvable ; fichier de playlist du player (version 2) importé tel quel ;
  6. ce qui suppose un FICHIER les laisse de côté : intégrité (ni « hors ligne » ni « sans
     vignette »), liste des médias des macros ; suppression : son contenu part avec elle ;
  7. projet exporté / importé : le contenu voyage, les entrées suivent les médias retrouvés ;
  8. macros : charger une playlist dans un player (players seulement, playlists du projet) ;
  9. routes et droits : l'exploitant (pilote des plugins) enregistre, le lecteur non ; importer
     exige la gestion des médias ; une fiche qui n'est pas une playlist est refusée.

Base jetable (jamais la base de prod).

    ./venv/bin/python tests/verif_media_playlists.py
"""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_playlists_")
import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(TMP, "principale.db")                     # jamais la base de prod
database.init_db()
from app import media_registry as mr                                      # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")
os.makedirs(mr.THUMB_DIR, exist_ok=True)

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


c = mr.db()
NOW = "2026-11-07T10:00:00"


U = {k: "%08d-0000-4000-8000-000000000000" % n for n, k in enumerate(("A", "B", "C", "B2", "B3", "INCONNU", "DISPARU"), 1)}
A, B, C, B2, B3, INCONNU, DISPARU = (U[k] for k in ("A", "B", "C", "B2", "B3", "INCONNU", "DISPARU"))   # format de la base


def media(aid, name, dur, fp):
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, tech_json, meta_json) "
              "VALUES (?,?,?,?,?,?,?,?)", (aid, name, NOW, NOW, "import", fp,
                                           json.dumps({"duration": dur, "video": {"width": 1920}}), "{}"))
    c.execute("INSERT INTO media_files(node_id, path, size, asset_id, kind, state) VALUES (1,?,10,?,'main','present')",
              ("p/" + name, aid))
    c.commit()


media(A, "a.mxf", 10.0, "fpA")
media(B, "b.mxf", 20.0, "fpB")
media(C, "c.mxf", 30.0, "fpC")

print("1. Créer")
P = mr.playlist_create("Matinale", [{"asset": A, "in": 2}, {"asset": B, "out": "5"}, {"file": ""}, "n'importe quoi",
                                    {"asset": C, "loop": 1, "pause": 0, "inutile": 3}])
row = c.execute("SELECT * FROM media_assets WHERE id=?", (P,)).fetchone()
tech = json.loads(row["tech_json"])
verifier(row["name"] == "Matinale" and row["origin"] == "playlist" and tech["kind"] == "playlist", "fiche de type playlist")
items = mr.playlist_items(P)
verifier([e["asset"] for e in items] == [A, B, C] and items[1]["out"] == 5.0 and items[2]["loop"] is True
         and "inutile" not in items[2], "entrées normalisées, les vides et les illisibles écartées (%s)" % items)
verifier(tech["items"] == 3 and tech["duration"] == 8 + 5 + 30, "durée JOUÉE : 8 + 5 + 30 = 43 s (%s)" % tech["duration"])
verifier(not mr.is_playlist(A) and mr.is_playlist(P), "is_playlist distingue un média d'une playlist")

print("2. Dans le browser")
k = lambda kinds: {a["id"] for a in mr.search({"kinds": kinds})}
verifier(k(["playlist"]) == {P}, "type « playlist » : elle seule (%s)" % k(["playlist"]))
verifier(P not in k(["video", "audio"]), "les types vidéo / audio ne la montrent pas (bibliothèque d'un player d'avant)")
lst = {a["id"]: a for a in mr.list_assets()}
verifier(lst[P]["tech"]["duration"] == 43 and lst[P]["copies"] == [], "liste : sa durée, aucune copie")

print("3. Lire")
mr.playlist_save(P, mr.playlist_items(P) + [{"asset": DISPARU}])
g = mr.playlist_get(P)
verifier([e.get("name") for e in g["items"][:3]] == ["a.mxf", "b.mxf", "c.mxf"] and g["items"][0]["duration"] == 10.0,
         "nom et durée de chaque média")
verifier(g["items"][3].get("missing") is True, "média disparu : entrée gardée et signalée")
verifier(mr.playlist_get(A) is None, "une fiche de média n'est pas lue comme une playlist")

print("4. Enregistrer")
mr.playlist_save(P, [{"asset": C}, {"asset": A}], name="Matinale v2")
row = c.execute("SELECT name, tech_json FROM media_assets WHERE id=?", (P,)).fetchone()
verifier(row["name"] == "Matinale v2" and [e["asset"] for e in mr.playlist_items(P)] == [C, A]
         and json.loads(row["tech_json"])["duration"] == 40, "contenu, nom et durée mis à jour")
try:
    mr.playlist_save(A, [])
    verifier(False, "enregistrer une playlist sur un média : refusé")
except ValueError:
    verifier(True, "enregistrer une playlist sur un média : refusé")

print("5. Exporter, importer ailleurs")
mr.playlist_save(P, [{"asset": A, "in": 1}, {"asset": B}, {"asset": INCONNU, "file": "x/y.mov"}])
ex = mr.playlist_export(P)
verifier(ex["format"] == "bobi-playlist" and ex["items"][0]["fingerprint"] == "fpA" and ex["items"][0]["name"] == "a.mxf",
         "export : identité, nom et empreinte de chaque média")
def renomme(old, new, name, dur, fp):                                     # « autre serveur » : autre identité
    c.execute("DELETE FROM media_files WHERE asset_id=?", (old,))
    c.execute("DELETE FROM media_assets WHERE id=?", (old,))
    c.commit()
    media(new, name, dur, fp)


renomme(B, B2, "b.mxf", 20.0, "fpB")
nid, rep = mr.playlist_import(json.loads(json.dumps(ex)), bin_id=None)
it = mr.playlist_items(nid)
verifier([e["asset"] for e in it] == [A, B2, INCONNU] and it[0]["in"] == 1.0,
         "A par identité, B par empreinte (→ B2), l'inconnu gardé (%s)" % [e["asset"] for e in it])
verifier(rep == {"items": 3, "by_id": 1, "by_fingerprint": 1, "unresolved": 1}, "compte rendu : %s" % rep)
old = {"name": "Ancienne", "version": 2, "items": [{"asset": C, "file": "Projet/c.mxf", "title": C, "in": None, "out": 4,
                                                   "loop": False, "pause": True}]}
nid2, _ = mr.playlist_import(old)
verifier(c.execute("SELECT name FROM media_assets WHERE id=?", (nid2,)).fetchone()["name"] == "Ancienne"
         and mr.playlist_items(nid2)[0]["pause"] is True, "fichier de playlist du player (version 2) : importé tel quel")
for bad in ({"pas": "d'items"}, "texte"):
    try:
        mr.playlist_import(bad)
        verifier(False, "import illisible refusé (%r)" % (bad,))
    except ValueError:
        verifier(True, "import illisible refusé (%r)" % (bad,))

print("6. Ce qui suppose un fichier")
rep = mr.integrity()
off = {x["id"] for x in rep["offline"]}
verifier(not ({P, nid, nid2} & off) and rep["no_thumb"] == 3, "intégrité : ni « hors ligne » ni « sans vignette » (%s, %d)"
         % (off, rep["no_thumb"]))
opts = {o["value"] for o in mr.macro_asset_options()}
verifier(P not in opts and A in opts, "liste des médias des macros : sans les playlists")
mr.delete_asset(nid2, with_files=True)
verifier(c.execute("SELECT COUNT(*) FROM media_playlists WHERE asset_id=?", (nid2,)).fetchone()[0] == 0,
         "playlist supprimée : son contenu avec elle")

print("7. Projets")
PR = database.db_import_project("Régie", [])
mr.playlist_save(P, [{"asset": A}, {"asset": B2}])
for x in (P, A, B2):
    mr.link_project(x, PR)
verifier({o["value"] for o in mr.macro_asset_options(pid=PR)} == {A, B2},
         "liste des médias des macros, d'un projet : sans sa playlist")
exp = mr.project_export_media(PR)
rp = next(r for r in exp["media"] if r["id"] == P)
verifier([e["asset"] for e in rp.get("playlist_items") or []] == [A, B2], "export de projet : le contenu voyage")
mr.delete_asset(P, with_files=False)
renomme(B2, B3, "b.mxf", 20.0, "fpB")                                 # retrouvé par empreinte sous une autre identité
n = mr.project_import_media(PR, exp["media"], exp["media_fields"])
verifier([e["asset"] for e in mr.playlist_items(n["ids"][P])] == [A, B3],
         "import de projet : contenu recréé, l'entrée suit le média retrouvé (%s)" % mr.playlist_items(n["ids"][P]))

print("8. Macros : charger une playlist dans un player")
posts = []
mr._post_player = lambda vmid, path, body: posts.append((vmid, path, body))
mr._macro_containers = lambda pid=None: [{"value": "900", "label": "Atelier-player (player)"},
                                         {"value": "901", "label": "Atelier-stills (stills)"}]
opt_c = mr.action_options("load_playlist", "container", None)
verifier([o["value"] for o in opt_c] == ["900"], "liste des conteneurs : les players seulement (%s)" % opt_c)
PLM = mr.playlist_create("Pour macro", [{"asset": A, "in": 1}, {"asset": C}], project_id=PR)
verifier({o["value"] for o in mr.macro_playlist_options(PR)} == {n["ids"][P], PLM}
         and PLM not in {o["value"] for o in mr.macro_playlist_options(PR + 1)}, "liste des playlists : celles du projet de la macro")
r = mr.run_action("load_playlist", {"container": "900", "playlist": PLM, "play": True}, {"project_id": PR})
paths = [x[1] for x in posts]
verifier(paths == ["/playlist", "/pl_mark", "/plgoto"] and posts[0][2]["files"][0]["asset"] == A and posts[0][2]["files"][0]["in"] == 1.0
         and posts[0][2]["files"][0]["title"] == "a.mxf", "playlist envoyée au player, puis marquée, puis lancée (%s)" % paths)
verifier(posts[1][2] == {"id": PLM, "name": "Pour macro"} and posts[2][2] == {"index": 0, "play": True} and r["playing"] is True,
         "marque = la fiche ; lancement sur le premier élément")
posts.clear()
mr.run_action("load_playlist", {"container": "900", "playlist": PLM}, {"project_id": PR})
verifier([x[1] for x in posts] == ["/playlist", "/pl_mark"], "sans « lancer » : chargée, pas lancée")
for prm, why in (({"container": "901", "playlist": PLM}, "un conteneur qui n'est pas un player"),
                 ({"container": "900", "playlist": PLM}, "une playlist hors du projet de la macro")):
    try:
        mr.run_action("load_playlist", prm, {"project_id": PR + 1 if "hors" in why else PR})
        verifier(False, "refusé : " + why)
    except RuntimeError:
        verifier(True, "refusé : " + why)

print("9. Routes et droits")
from app import auth                                                     # noqa: E402
from app.auth import hash_password                                       # noqa: E402
pw = hash_password("Banc-Motdepasse-2026!")
for u, role in (("adm", "admin"), ("expl", "exploitant"), ("lect", "viewer")):
    database.db_create_user(u, pw, role)
uid = {u: database.get_db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()["id"] for u in ("adm", "expl", "lect")}
import main as app_main                                                  # noqa: E402
flask_app = app_main.app
flask_app.config["TESTING"] = True
cli = flask_app.test_client()


def as_(who):
    with cli.session_transaction() as ss:
        ss.clear()
        ss["user_id"], ss["username"] = uid[who], who
        r = database.get_db().execute("SELECT session_epoch FROM users WHERE id=?", (uid[who],)).fetchone()
        ss["session_epoch"] = (r["session_epoch"] if r else 0) or 0


database.db_set_project_member(PR, uid["expl"], "operator")
auth.check_vmid_access = lambda vmid, min_role="viewer": None
mr.container_context = lambda vmid: {"vmid": vmid, "node_id": 1, "project_id": PR, "project_name": "Régie"}
as_("expl")
r = cli.post("/api/media/playlists", json={"name": "Hors projet", "items": []})
verifier(r.status_code == 400 and "projet" in r.get_json()["error"],
         "exploitant, hors projet : refusé (la fiche lui serait aussitôt invisible)")
r = cli.post("/api/media/playlists", json={"name": "Du player", "items": [{"asset": A}], "vmid": 900})
verifier(r.status_code == 200 and mr.is_playlist(r.get_json()["id"]), "exploitant : enregistre une playlist depuis un player")
verifier(PR in mr.get_asset(r.get_json()["id"])["projects"], "… rangée dans le projet du player")
pid = r.get_json()["id"]
r = cli.put("/api/media/playlists/%s" % pid, json={"items": [{"asset": C}, {"asset": A}]})
verifier(r.status_code == 200 and [e["asset"] for e in mr.playlist_items(pid)] == [C, A], "… et la réenregistre")
verifier(cli.put("/api/media/playlists/%s" % nid, json={"items": []}).status_code == 404,
         "playlist HORS de ses projets : ni vue ni modifiée (404)")
verifier(cli.put("/api/media/playlists/%s" % A, json={"items": []}).status_code == 400, "PUT sur un média (pas une playlist) : 400")
verifier(cli.post("/api/media/playlists/import", json={"data": ex}).status_code == 403,
         "importer un fichier : réservé à la gestion des médias (403)")
g = cli.get("/api/media/playlists/%s" % pid).get_json()
verifier([e.get("name") for e in g["items"]] == [None, "a.mxf"] and g["items"][0].get("hidden") is True,
         "lecture par l'API : un média HORS de ses projets n'est pas nommé (%s)" % g["items"])
e = cli.get("/api/media/playlists/%s/export" % pid)
verifier(e.status_code == 200 and "attachment" in e.headers.get("Content-Disposition", "")
         and e.get_json()["format"] == "bobi-playlist", "export : fichier à télécharger")
as_("lect")
verifier(cli.post("/api/media/playlists", json={"name": "x", "items": []}).status_code == 403, "lecteur : ne crée pas (403)")
verifier(cli.put("/api/media/playlists/%s" % pid, json={"items": []}).status_code == 403, "lecteur : ne modifie pas (403)")
as_("adm")
r = cli.post("/api/media/playlists/import", json={"data": ex, "name": "Importée"})
verifier(r.status_code == 200 and r.get_json()["report"]["items"] == 3, "admin : import accepté, compte rendu")

print()
if ECHECS:
    print("%d échec(s)" % len(ECHECS))
    sys.exit(1)
print("Playlists dans la base : tous les cas tiennent.")
