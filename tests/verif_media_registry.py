#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du REGISTRE DES MÉDIAS (app/media_registry.py) — sans nœud : l'analyse sur le nœud est
# simulée (le script de nœud, lui, est exercé à part sur des fichiers réels, cf. fin du fichier).
#
# ★ CE QUE ÇA PROTÈGE : l'identité d'un média doit survivre au déplacement et à la copie, et rien
# ne doit entrer dans la base sans import explicite.
#
# ★ CE QUI EST VÉRIFIÉ :
#   1. un fichier vu sur un volume reste NON RÉFÉRENCÉ tant qu'on ne l'importe pas ;
#   2. l'import crée le média, demande l'écriture de l'identité dans le sidecar et la vignette ;
#   3. déplacé sur le même nœud (sidecar porteur de l'identité) : même média, l'ancien
#      emplacement est oublié, pas signalé manquant ;
#   4. copié sur un autre nœud SANS sidecar : rattaché par l'empreinte ;
#   5. disparu : la copie passe `missing`, le média et ses métadonnées restent ; sans plus aucune
#      copie, il est « hors ligne » dans le rapport d'intégrité ;
#   6. un fichier non référencé qui disparaît est simplement oublié ;
#   7. champs personnalisés : seuls les champs déclarés (et ceux de base) sont acceptés ;
#   8. export → import « fusion » dans une base vierge : médias, métadonnées, rattachements aux
#      projets repris ; les EMPLACEMENTS ne le sont pas (ils n'ont de sens que sur l'origine) ; le
#      média est ensuite retrouvé sur un volume par la seule identité de son sidecar ;
#  10. recherche (liste blanche de champs, dates relatives, champs personnalisés, OU) et bins
#      (intelligent réévalué à chaque lecture, manuel, filtre dans un bin) ;
#  11. clips du recorder importés d'eux-mêmes (identité écrite par le recorder), boucle exclue,
#      empreinte posée à la fermeture ;
#  13. sorties du transcodeur : VERSIONS du média source (pas de fiche à part), traitement
#      interrogeable, projet du producteur, entrée sans identité retrouvée par l'empreinte ;
#  12. copie vérifiée vers un nœud (sidecar compris, empreinte contrôlée, réutilisée si présente),
#      reprise unique des anciens bins du Gestionnaire ;
#  14. un projet emporte ses médias : fiches (identité, métadonnées, vignette, champs utilisés),
#      reconnues sur un autre serveur (identité, sinon empreinte ; la fiche locale garde la main),
#      hors ligne sans copie ; archive en flux (projet en tête, fichiers entiers, sidecars), dépôt
#      dans le dossier du nouveau projet sans écraser un homonyme ;
#   9. le script de nœud (script_templates/media_scan.py) relève, empreinte et tamponne de vrais
#      fichiers : identité insérée dans un sidecar existant, sidecar créé sinon.
#
#   $ ./venv/bin/python tests/verif_media_registry.py
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_")
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
    if cond:
        ok += 1
        print("  ✓", msg)
    else:
        fail += 1
        print("  ✗", msg)


# ── nœuds simulés : un volume = dict chemin → {size, mtime, fp, id} ─────────────────────────────
VOLUMES = {1: {}, 2: {}}
STAMPS = []


def fake_run(node, req, timeout=0):
    vol = VOLUMES[node["id"]]
    if req["mode"] == "scan":
        return {"files": [dict({"path": p}, size=f["size"], mtime=f["mtime"], fp=f["fp"],
                               **({"id": f["id"]} if f.get("id") else {}),
                               **{k: f[k] for k in ("origin", "loop", "growing", "producer", "derived_from", "process", "version") if f.get(k)},
                               **({"tech": {"duration": 10.0, "video": {"width": 1920}}} if not req["known"].get(p) else {}))
                          for p, f in vol.items()], "probed": 0}
    res = {"stamped": {}, "thumbs": {}}
    for it in req.get("stamp") or []:
        STAMPS.append((node["id"], it["path"], it["id"]))
        vol[it["path"]]["id"] = it["id"]
        if it.get("version"):
            vol[it["path"]]["version"] = it["version"]
        res["stamped"][it["path"]] = "stamped"
    for it in req.get("thumbs") or []:
        res["thumbs"][it["id"]] = "/9j/AA=="                       # quelques octets JPEG factices
    return res


mr._run_on_node = fake_run
mr._node = lambda nid: {"id": int(nid), "name": "n%s" % nid, "media_image": "img"}
import app.database as _database                                       # noqa: E402
_database.db_get_nodes = lambda: [{"id": 1, "name": "n1", "media_image": "img"},
                                  {"id": 2, "name": "n2", "media_image": "img"}]

print("1. non référencé tant qu'on n'importe pas")
VOLUMES[1]["a/clip.mxf"] = {"size": 100, "mtime": 1, "fp": "fpA"}
VOLUMES[1]["b/autre.mov"] = {"size": 200, "mtime": 1, "fp": "fpB"}
s = mr.scan_node(1)
check(s["unreferenced"] == 2 and not mr.list_assets(), "2 fichiers vus, 0 média")

print("2. import")
ids = mr.import_files(1, ["a/clip.mxf"], project_id=7)
aid = ids[0]
a = mr.get_asset(aid)
check(a and a["name"] == "clip.mxf" and a["projects"] == [7], "média créé, rattaché au projet 7")
check((1, "a/clip.mxf", aid) in STAMPS, "identité écrite dans le sidecar")
check(os.path.exists(mr.thumb_path(aid)), "vignette stockée centralement")

print("3. déplacé sur le même nœud")
VOLUMES[1]["c/renomme.mxf"] = VOLUMES[1].pop("a/clip.mxf")
s = mr.scan_node(1)
a = mr.get_asset(aid)
check([c["path"] for c in a["copies"]] == ["c/renomme.mxf"], "une seule copie, au nouvel emplacement")
check(s["missing"] == 0 and s.get("moved") == 1, "compté comme déplacé, pas manquant")

print("4. copié sur un autre nœud sans sidecar")
VOLUMES[2]["x/copie.mxf"] = {"size": 100, "mtime": 5, "fp": "fpA"}
mr.scan_node(2)
a = mr.get_asset(aid)
check(sorted(c["node_id"] for c in a["copies"]) == [1, 2], "deux copies du même média")
check((2, "x/copie.mxf", aid) in STAMPS, "identité tamponnée sur la copie")

print("5. disparu")
VOLUMES[1].pop("c/renomme.mxf")
VOLUMES[2].pop("x/copie.mxf")
mr.scan_node(1)
mr.scan_node(2)
a = mr.get_asset(aid)
check(a and all(c["state"] == "missing" for c in a["copies"]), "copies `missing`, média conservé")
rep = mr.integrity()
check([o["id"] for o in rep["offline"]] == [aid] and len(rep["missing_copies"]) == 2,
      "intégrité : 1 média hors ligne, 2 copies manquantes")
mr.forget_copy(aid, 2, "x/copie.mxf")
check(len(mr.get_asset(aid)["copies"]) == 1, "oublier une copie absente")

print("6. non référencé disparu → oublié")
VOLUMES[1].pop("b/autre.mov")
mr.scan_node(1)
check(not mr.integrity()["unreferenced"], "plus de trace")

print("7. champs personnalisés")
mr.save_field("emission", "Émission", "text")
a = mr.update_meta(aid, {"title": "Ouverture", "emission": "JT 20h", "inconnu": "x"})
check(a["meta"] == {"title": "Ouverture", "emission": "JT 20h"}, "champ déclaré accepté, inconnu ignoré")
try:
    mr.save_field("title", "x", "text")
    check(False, "clé de base refusée")
except ValueError:
    check(True, "clé de base refusée")

print("8. export → import fusion dans une base vierge")
data = mr.export_bytes()
mr.MEDIA_DB_PATH = os.path.join(TMP, "autre.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs2")
res = mr.import_bytes(data, mode="merge")
b = mr.get_asset(aid)
check(b and b["meta"].get("emission") == "JT 20h" and b["projects"] == [7], "métadonnées et projet repris")
check(res["imported"]["files"] == 0 and b["copies"] == [], "emplacements NON repris")
check(os.path.exists(mr.thumb_path(aid)), "vignette reprise")
# Sur le nouveau serveur, le média est retrouvé par la SEULE identité de son sidecar (empreinte
# différente : ré-enveloppé, ou empreinte jamais calculée sur l'origine).
VOLUMES[1]["z/arrive.mxf"] = {"size": 999, "mtime": 9, "fp": "fpAutre", "id": aid}
mr.scan_node(1)
check([c["path"] for c in mr.get_asset(aid)["copies"]] == ["z/arrive.mxf"], "copie retrouvée par l'identité du sidecar")

print("10. recherche et bins")
import time as _t                                                          # noqa: E402
c = mr.db()
old = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(_t.time() - 3 * 86400))
now = _t.strftime("%Y-%m-%dT%H:%M:%S")
for i, (name, h, dur, na, created, meta) in enumerate([
        ("uhd_a.mxf", 2160, 20, 1, now, {"emission": "JT 20h"}),
        ("hd_b.mov", 1080, 300, 8, now, {"emission": "Météo"}),
        ("vieux.mp4", 1080, 60, 2, old, {})]):
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, tech_json, meta_json) VALUES (?,?,?,?,?,?)",
              ("S%d" % i, name, created, created,
               json.dumps({"duration": dur, "video": {"height": h, "width": h * 16 // 9}, "audio": [{"channels": 1}] * na}),
               json.dumps(meta)))
c.commit()
ids = lambda r: sorted(a["id"] for a in r)
check(ids(mr.search({"all": [{"field": "height", "op": "gte", "value": 2160}]})) == ["S0"], "définition ≥ 2160")
recent = set(ids(mr.search({"all": [{"field": "created_at", "op": "within", "value": {"n": 1, "unit": "hours"}}]})))
check({"S0", "S1"} <= recent and "S2" not in recent, "créés dans la dernière heure (le vieux exclu)")
check(ids(mr.search({"all": [{"field": "audio_tracks", "op": "gt", "value": 4}]})) == ["S1"], "plus de 4 pistes audio")
check(ids(mr.search({"all": [{"field": "meta.emission", "op": "contains", "value": "météo"}]})) == ["S1"], "champ personnalisé, sans casse")
check(ids(mr.search({"any": [{"field": "height", "op": "eq", "value": 2160}, {"field": "duration", "op": "gt", "value": 200}]})) == ["S0", "S1"], "OU")
try:
    mr.search({"all": [{"field": "a.name; DROP TABLE media_assets", "op": "eq", "value": 1}]})
    check(False, "champ hors liste refusé")
except ValueError:
    check(True, "champ hors liste refusé")
smart = mr.save_bin("Dernière heure", query={"all": [{"field": "created_at", "op": "within", "value": {"n": 1, "unit": "hours"}}]})
check("S2" not in ids(mr.bin_assets(smart)) and "S1" in ids(mr.bin_assets(smart)), "bin intelligent réévalué")
c.execute("UPDATE media_assets SET created_at=? WHERE id='S1'", (old,))
c.commit()
check("S1" not in ids(mr.bin_assets(smart)), "… et suit le temps (S1 vieilli en sort)")
man = mr.save_bin("Sélection")
mr.bin_add(man, ["S0", "S2"])
check(ids(mr.bin_assets(man)) == ["S0", "S2"], "bin manuel")
check(ids(mr.bin_assets(man, extra={"all": [{"field": "height", "op": "eq", "value": 1080}]})) == ["S2"], "filtre dans un bin")
try:
    mr.bin_add(smart, ["S0"])
    check(False, "on ne range pas dans un bin intelligent")
except ValueError:
    check(True, "on ne range pas dans un bin intelligent")
mr.delete_bin(man)
check(mr.get_asset("S0") is not None, "supprimer un bin ne supprime pas ses médias")

print("11. clips du recorder : entrés d'eux-mêmes")
VOLUMES[1]["rec/clip_001.mxf"] = {"size": 10, "mtime": 20, "fp": None, "id": "REC-1", "origin": "recorder", "growing": True,
                                  "producer": "recorder-banc"}
VOLUMES[1]["rec/boucle.mxf"] = {"size": 10, "mtime": 20, "fp": None, "id": "LOOP-1", "origin": "recorder", "loop": True, "growing": True}
s11 = mr.scan_node(1)
check(mr.get_asset("REC-1") is not None and s11.get("auto_imported") == 1, "clip en cours importé automatiquement")
check(mr.get_asset("LOOP-1") is None, "tampon de boucle non sauvegardé laissé dehors")
check(not any(p == "rec/clip_001.mxf" for (_n, p, _i) in STAMPS), "sidecar du recorder jamais tamponné (il le réécrit)")
VOLUMES[1]["rec/clip_001.mxf"].update(size=500, mtime=30, fp="fpREC", growing=False)
mr.scan_node(1)
check(mr.get_asset("REC-1")["fingerprint"] == "fpREC", "empreinte posée à la fermeture")
check([a["id"] for a in mr.search({"all": [{"field": "producer", "op": "eq", "value": "recorder-banc"}]})] == ["REC-1"],
      "producteur retenu et interrogeable (bibliothèque d'un recorder)")

print("13. sorties du transcodeur : VERSIONS du média source (pas de fiche à part), traitement, projet")
mr.update_meta("S0", {"title": "Match", "emission": "Sport", "keywords": "foot"})
_pp = mr._producer_project
mr._producer_project = lambda h: 9 if h == "tc-1" else None
n_assets = len(mr.list_assets())
VOLUMES[1]["out/match__s0_h264proxy.mp4"] = {"size": 30, "mtime": 40, "fp": "fpTC", "id": "S0", "version": "h264proxy",
                                     "origin": "transcoder", "producer": "tc-1",
                                     "process": {"preset": "h264_proxy", "version": "h264proxy", "version_label": "H.264 Proxy",
                                                 "loudness": {"measured_lufs": -21.4, "true_peak_dbtp": -1.2},
                                                 "result": {"realtime_factor": 6.5}, "submitted_by": "operateur1"}}
mr.scan_node(1)
t = mr.get_asset("S0")
vs = {v["key"]: v for v in t["versions"]}
check(len(mr.list_assets()) == n_assets and "h264proxy" in vs, "sortie rangée sous la fiche de sa source : une version, pas un média")
check(vs["h264proxy"]["label"] == "H.264 Proxy" and vs["h264proxy"]["fingerprint"] == "fpTC"
      and vs["h264proxy"]["process"]["loudness"]["measured_lufs"] == -21.4, "version : libellé, empreinte, traitement complet")
check(t["meta"]["title"] == "Match", "champs de la FICHE (une seule fiche, toutes versions)")
check(t["default_version"] == "h264proxy", "original jamais vu sur un volume : la version par défaut est la seule version")
check([c["version"] for c in t["copies"] if c["path"] == "out/match__s0_h264proxy.mp4"] == ["h264proxy"], "la copie dit sa version")
q = lambda cs: [a["id"] for a in mr.search({"all": cs})]
check(q([{"field": "loudness_lufs", "op": "gt", "value": -22}]) == ["S0"] and q([{"field": "submitted_by", "op": "eq", "value": "operateur1"}]) == ["S0"],
      "traitement d'une version interrogeable (loudness, demandeur) : la fiche répond")
check(9 in t["projects"], "rattachée au projet du transcodeur")
c.execute("UPDATE media_assets SET fingerprint='fpS0' WHERE id='S0'")
c.commit()
# Entrée SANS identité (dossier de dépôt) : le transcodeur tire l'identité de l'empreinte (orpheline
# ici) ; la base retrouve la source par l'empreinte de l'entrée et range la sortie sous elle.
VOLUMES[1]["out/depot__d0d0d0_h264.mp4"] = {"size": 31, "mtime": 41, "fp": "fpTC2", "id": "d0d0d0d0-0000-5000-8000-000000000000",
                                     "version": "h264", "origin": "transcoder", "producer": "tc-1",
                                     "process": {"preset": "h264_standard", "version": "h264", "source": {"fingerprint": "fpS0"},
                                                 "watchfolder": {"name": "Ingest", "file": "match.mxf"}}}
mr.scan_node(1)
t2 = mr.get_asset("S0")
check(any(v["key"] == "h264" for v in t2["versions"]) and mr.get_asset("d0d0d0d0-0000-5000-8000-000000000000") is None,
      "entrée sans identité : sortie rangée sous la source retrouvée par l'empreinte, aucune fiche orpheline")
check(VOLUMES[1]["out/depot__d0d0d0_h264.mp4"]["id"] == "S0", "… et son sidecar ré-identifié (identité orpheline remplacée)")
mr._producer_project = _pp

print("12. copie vérifiée, rapatriement, reprise des anciens bins")
_copies = []


def fake_stream(sh, sa, dh, da, timeout):
    _copies.append((sa, da))
    if sa.endswith(".xml"):
        return True, ""
    # Le fichier « arrive » sur le nœud cible (volume simulé), avec la même empreinte et l'identité.
    src_vol = VOLUMES[1 if sh == "h1" else 2]
    rel_src = sa.split("/vol/", 1)[1]
    f = dict(src_vol[rel_src]); f["id"] = "S0" if FP_OK else "S0"
    if not FP_OK:
        f["fp"] = "fpAbime"
    VOLUMES[2 if dh == "h2" else 1][da.split("/vol/", 1)[1].replace(".partial", "")] = f
    return True, ""


FP_OK = True
mr._ssh_stream = fake_stream
mr._node = lambda nid: {"id": int(nid), "name": "n%s" % nid, "media_image": "img", "host": "h%s" % nid, "media_mount": "/vol"}
import app.node_driver as _nd                                           # noqa: E402
_nd.host_exec = lambda node, cmd, input_data=None, timeout=0: (1 if cmd.startswith("test -e") else 0, "", "")
c.execute("UPDATE media_assets SET fingerprint='fpS0' WHERE id='S0'")
c.commit()
VOLUMES[1]["s0/uhd_a.mxf"] = {"size": 77, "mtime": 5, "fp": "fpS0", "id": "S0"}
mr.scan_node(1)
from app import media_naming as _mn                                     # noqa: E402
S0C = _mn.coded_name("s0/uhd_a.mxf", "S0")      # une copie est un fichier CRÉÉ par le système : codée
rel = mr.copy_asset("S0", 2)
a = mr.get_asset("S0")
check(a["default_version"] == "original", "original vu : il redevient la version par défaut")
check(rel == S0C and sorted(x["node_id"] for x in a["copies"] if x["state"] == "present" and x["version"] == "original") == [1, 2],
      "copie vers un autre nœud, rattachée au même média")
check(any(x[0].endswith(".xml") for x in _copies), "le sidecar (identité) voyage avec le fichier")
check(mr.copy_asset("S0", 2) == S0C and len([x for x in _copies if not x[0].endswith(".xml")]) == 1,
      "copie déjà présente : réutilisée, rien de recopié")
FP_OK = False
VOLUMES[1]["s0/autre.mxf"] = {"size": 5, "mtime": 5, "fp": "fpS0", "id": "S0"}
try:
    mr.copy_asset("S0", 2, "s0/abime.mxf")
    check(False, "copie abîmée détectée")
except RuntimeError as e:
    check("empreinte" in str(e), "copie abîmée détectée (empreinte)")
FP_OK = True
# Copie arrivée mais SANS empreinte (prise pour un fichier en cours d'écriture) : ce n'est pas un
# succès — la vérification n'a pas eu lieu (cas réel du premier essai, date « maintenant »).
_orig_stream = mr._ssh_stream
def stream_sans_fp(sh, sa, dh, da, timeout):
    ok = _orig_stream(sh, sa, dh, da, timeout)
    if not sa.endswith(".xml"):
        VOLUMES[2][da.split("/vol/", 1)[1].replace(".partial", "")].update(fp=None, growing=True)
    return ok
mr._ssh_stream = stream_sans_fp
try:
    mr.copy_asset("S0", 2, "s0/sans_fp.mxf")
    check(False, "copie non vérifiable refusée")
except RuntimeError as e:
    check("vérification impossible" in str(e), "copie non vérifiable refusée (pas d'empreinte)")
mr._ssh_stream = _orig_stream
mr.container_view = lambda vmid: (2, "proj/")
got = mr.fetch_for_container("S0", 99)
check(got == S0C and any(x["node_id"] == 2 and x["path"] == "proj/" + S0C for x in mr.get_asset("S0")["copies"]),
      "rapatriement : copié DANS le dossier du projet, chemin rendu tel que le conteneur le voit")
g = _database.get_db()
g.execute("CREATE TABLE media_bins (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER, created_at TEXT)")
g.execute("CREATE TABLE media_bin_items (bin_id INTEGER, node_id INTEGER, path TEXT, kind TEXT, added_at TEXT)")
g.execute("INSERT INTO media_bins VALUES (10, 'Ancien', NULL, ''), (11, 'Sous', 10, '')")
g.execute("INSERT INTO media_bin_items VALUES (11, 1, 's0/uhd_a.mxf', 'clip', ''), (11, 1, 'inconnu.mov', 'clip', '')")
g.commit()
res = mr.migrate_legacy_bins()
bins = {b["name"]: b for b in mr.list_bins()}
check(res == {"bins": 2, "items": 1, "skipped": 1} and bins["Sous"]["parent_id"] == bins["Ancien"]["id"],
      "anciens bins repris (hiérarchie), fichier hors base compté")
check(mr.migrate_legacy_bins() is None, "reprise faite une seule fois")

print("14. un projet emporte ses médias (fiches, puis archive avec les fichiers)")
import io                                                               # noqa: E402
import tarfile                                                          # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "proj_origine.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs_po")
VOLUMES[1].clear()
VOLUMES[2].clear()
mr._run_on_node = fake_run
mr._node = lambda nid: {"id": int(nid), "name": "n%s" % nid, "media_image": "img", "host": "h%s" % nid}
_orig_gp = _database.db_get_project
_database.db_get_project = lambda pid: {"id": pid, "media_path": "/srv/mxl-media/proj%s" % pid}
mr.save_field("emission", "Émission", "text")
mr.save_field("hors_projet", "Hors projet", "text")
VOLUMES[1]["proj3/rush/un.mxf"] = {"size": 1000, "mtime": 11, "fp": "fpP1"}
VOLUMES[1]["proj3/deux.wav"] = {"size": 300, "mtime": 12, "fp": "fpP2"}
VOLUMES[1]["autre.mov"] = {"size": 50, "mtime": 13, "fp": "fpP3"}
mr.scan_node(1)
p1, p2 = mr.import_files(1, ["proj3/rush/un.mxf", "proj3/deux.wav"], project_id=3)
p3 = mr.import_files(1, ["autre.mov"], project_id=99)[0]
mr.update_meta(p1, {"emission": "JT 13h"})
mr.update_meta(p3, {"hors_projet": "x"})
exp = mr.project_export_media(3)
check(sorted(r["id"] for r in exp["media"]) == sorted([p1, p2]), "le projet exporte ses médias, pas ceux d'un autre projet")
r1 = [r for r in exp["media"] if r["id"] == p1][0]
check(r1["meta"].get("emission") == "JT 13h" and r1.get("thumb_jpg") and r1["fingerprint"] == "fpP1",
      "fiche complète : métadonnées, vignette, empreinte")
check([f["key"] for f in exp["media_fields"]] == ["emission"], "seuls les champs personnalisés UTILISÉS voyagent")

# ── autre serveur : base vierge ──
mr.MEDIA_DB_PATH = os.path.join(TMP, "proj_cible.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs_pc")
VOLUMES[1].clear()
VOLUMES[2].clear()
mr.db().execute("INSERT INTO media_assets(id, name, created_at, updated_at, fingerprint, meta_json) VALUES ('deja-la-1', 'local.wav', 'x', 'x', 'fpP2', ?)",
                (json.dumps({"emission": "local gagne"}),))
mr.db().commit()
# La fiche entrante de deux.wav CONTREDIT la fiche locale sur un champ et en apporte un autre.
recs = [dict(r, meta={"emission": "importé écrase", "lieu": "Studio B"}) if r["id"] == p2 else r for r in exp["media"]]
recs += [{"id": "../../etc", "name": "piège"}]
n = mr.project_import_media(12, recs, exp["media_fields"])
check(n["new"] == 1 and n["known"] == 1 and n["skipped"] == 1, "1 nouveau, 1 reconnu par l'empreinte, 1 identité invalide écartée")
b1 = mr.get_asset(p1)
check(b1 and b1["projects"] == [12] and b1["meta"].get("emission") == "JT 13h" and os.path.exists(mr.thumb_path(p1)),
      "fiche, vignette et rattachement au NOUVEAU projet")
check(b1["copies"] == [] and mr.integrity()["offline"], "sans copie sur ce serveur : HORS LIGNE")
loc = mr.get_asset("deja-la-1")
check(n["ids"][p2] == "deja-la-1" and loc["meta"]["emission"] == "local gagne" and 12 in loc["projects"],
      "média déjà là : la fiche LOCALE garde la main, il rejoint le projet")
check(loc["meta"].get("lieu") == "Studio B", "… et un champ vide localement est complété")
check([f["key"] for f in mr.list_fields()] == ["emission"], "champ personnalisé recréé")
VOLUMES[2]["ailleurs/un.mxf"] = {"size": 1000, "mtime": 11, "fp": "fpP1"}
mr.scan_node(2)
check([c["path"] for c in mr.get_asset(p1)["copies"]] == ["ailleurs/un.mxf"], "la copie est retrouvée à l'analyse")

# ── archive « avec les médias » ──
mr.MEDIA_DB_PATH = os.path.join(TMP, "proj_origine.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs_po")
VOLUMES[1].clear()
VOLUMES[1]["proj3/rush/un.mxf"] = {"size": 1000, "mtime": 11, "fp": "fpP1", "id": p1}
VOLUMES[1]["proj3/deux.wav"] = {"size": 300, "mtime": 12, "fp": "fpP2", "id": p2}
VOLUMES[1]["autre.mov"] = {"size": 50, "mtime": 13, "fp": "fpP3", "id": p3}
CONTENU = {"proj3/rush/un.mxf": os.urandom(1000), "proj3/deux.wav": os.urandom(300)}
from app import node_driver as _nd                                      # noqa: E402
_nd_saved = (_nd.transfer_capable, _nd.host_exec)
_nd.transfer_capable = lambda n: True
_nd.host_exec = lambda n, cmd, **k: ((0, '<media id="X"/>', "") if cmd.startswith("cat ") and "un.mxf" in cmd
                                     else (1, "", ""))
mr._read_node_file = lambda node, rel, size: iter([CONTENU[rel][:400], CONTENU[rel][400:]])
plan = mr.project_archive_plan(3)
blob = b"".join(mr.project_archive_stream({"name": "P3", "media": exp["media"]}, plan))
with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
    names = tar.getnames()
    check(names[0] == "projet.bsproj.json" and json.loads(tar.extractfile(names[0]).read())["name"] == "P3",
          "archive : le projet EN TÊTE (lisible avant les fichiers)")
    check(tar.extractfile("media/%s/rush/un.mxf" % p1).read() == CONTENU["proj3/rush/un.mxf"],
          "fichier entier, chemin relatif au dossier du projet")
    check("media/%s/rush/un.mxf.xml" % p1 in names and "media/%s/deux.wav.xml" % p2 not in names,
          "sidecar joint quand il existe")
    check(not any(p3 in x for x in names), "un média d'un autre projet n'y est pas")
mr._read_node_file = lambda node, rel, size: iter([CONTENU[rel][:10]])
try:
    b"".join(mr.project_archive_stream({}, plan))
    check(False, "fichier tronqué : l'archive s'interrompt")
except RuntimeError:
    check(True, "fichier tronqué : l'archive s'interrompt (pas de faux livré)")

# Import de l'archive sur le nœud 2, dans le dossier du NOUVEAU projet ; un fichier homonyme déjà
# là n'est pas écrasé.
mr.MEDIA_DB_PATH = os.path.join(TMP, "proj_cible2.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs_pc2")
VOLUMES[2].clear()
VOLUMES[2]["proj20/deux.wav"] = {"size": 7, "mtime": 1, "fp": "fpAutreChose"}
tar_path = os.path.join(TMP, "import.tar")
with open(tar_path, "wb") as f:
    f.write(blob)
n = mr.project_import_media(20, exp["media"], exp["media_fields"])
POSES = {}


def fake_put(node, rel, fobj, size, mtime):
    data = fobj.read()
    POSES[rel] = data
    if not rel.endswith(".xml"):
        VOLUMES[node["id"]][rel] = {"size": len(data), "mtime": mtime, "fp": "fpP1" if data == CONTENU["proj3/rush/un.mxf"] else "fpP2"}


mr._put_on_node = fake_put
_nd.host_exec = lambda n, cmd, **k: (0, "", "") if "proj20/deux.wav" in cmd else (1, "", "")
job = mr.project_import_archive(20, tar_path, 2, n["ids"])
check(job["state"] == "done" and job["done"] == 2 and job["total"] == 2, "dépôt terminé : 2 fichiers sur 2")
UN = _mn.coded_name("proj20/rush/un.mxf", n["ids"][p1])                   # code de l'identité LOCALE
check(POSES.get(UN) == CONTENU["proj3/rush/un.mxf"] and UN + ".xml" in POSES,
      "posé dans le dossier du nouveau projet (nom codé), avec son sidecar")
check("proj20/deux.wav" not in POSES and any(k.startswith("proj20/deux__") and not k.endswith(".xml") for k in POSES),
      "homonyme déjà présent : NON écrasé, le nouveau porte son code")
check(not os.path.exists(tar_path), "archive effacée après dépôt")
check(sorted(c["path"] for c in mr.get_asset(p1)["copies"]) == [UN], "copie rattachée à l'analyse")
_nd.transfer_capable, _nd.host_exec = _nd_saved
_database.db_get_project = _orig_gp

print("9. script de nœud sur de vrais fichiers")
vol = os.path.join(TMP, "vol")
os.makedirs(os.path.join(vol, "d"))
with open(os.path.join(vol, "d", "un.mov"), "wb") as f:
    f.write(os.urandom(5 << 20))
with open(os.path.join(vol, "d", "deux.wav"), "wb") as f:
    f.write(os.urandom(1 << 20))
with open(os.path.join(vol, "d", "un.mov.xml"), "w") as f:
    f.write('<?xml version="1.0"?>\n<media status="closed">\n  <duration>3.0</duration>\n</media>\n')
os.utime(os.path.join(vol, "d", "un.mov"), (1, 1))
os.utime(os.path.join(vol, "d", "deux.wav"), (1, 1))
script = open(os.path.join(RACINE, "script_templates", "media_scan.py")).read().replace('ROOT = "/m"', 'ROOT = %r' % vol)


def run(req):
    p = subprocess.run([sys.executable, "-c", script], input=json.dumps(req).encode(), capture_output=True)
    return json.loads(p.stdout or b"{}")


r = run({"mode": "scan", "known": {}, "probe_max": 0})
fl = {f["path"]: f for f in r.get("files", [])}
check(set(fl) == {"d/un.mov", "d/deux.wav"} and all(len(f.get("fp", "")) == 32 for f in fl.values()),
      "deux fichiers relevés avec empreinte")
r = run({"mode": "stamp", "stamp": [{"path": "d/un.mov", "id": "ID-1"}, {"path": "d/deux.wav", "id": "ID-2"}]})
check(r["stamped"] == {"d/un.mov": "stamped", "d/deux.wav": "created"}, "sidecar complété / créé")
txt = open(os.path.join(vol, "d", "un.mov.xml")).read()
check('<media id="ID-1" status="closed">' in txt and "<duration>3.0</duration>" in txt, "sidecar existant intact, id inséré")
r = run({"mode": "scan", "known": {}, "probe_max": 0})
check({f["path"]: f.get("id") for f in r["files"]} == {"d/un.mov": "ID-1", "d/deux.wav": "ID-2"}, "identités relues")

# Vignette d'une IMAGE fixe : une seule trame — chercher à 1 s ne sort rien, et sur un JPEG même
# `-ss 0.0` ne sort rien (constaté le 2026-10-11 : aucune image de la base n'avait de vignette).
import shutil                                                           # noqa: E402
if shutil.which("ffmpeg"):
    for ext in ("jpg", "png"):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                        "-frames:v", "1", os.path.join(vol, "d", "image." + ext)], check=True)
    r = run({"mode": "stamp", "thumbs": [{"id": "J", "path": "d/image.jpg", "t": 1.0, "w": 160},
                                         {"id": "P", "path": "d/image.png", "t": 1.0, "w": 160}]})
    check(bool((r.get("thumbs") or {}).get("J")) and bool((r.get("thumbs") or {}).get("P")),
          "vignette d'une image fixe (JPEG et PNG), demandée à 1 s")
else:
    print("  (ffmpeg absent ici : vignette d'image fixe non vérifiée)")

print("\n%d contrôle(s) OK, %d en échec." % (ok, fail))
sys.exit(1 if fail else 0)
