#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# VERSIONS D'UN MÉDIA SOUS UNE SEULE FICHE (règle du 2026-10-16, app/media_registry.py § Versions).
#
# ★ CE QUE ÇA PROTÈGE : toutes les essences d'un média — l'original, ses transcodages, ses
# livraisons — sont des VERSIONS d'une même fiche (même identité, même code dans le nom de fichier,
# la clé de la version en suffixe). Une sortie du transcodeur n'ouvre jamais une fiche à part ; un
# contenu identique à une version est une copie de plus de CETTE version ; charger un média charge
# sa version par défaut, ou une version précise.
#
# ★ CE QUI EST VÉRIFIÉ (analyse des nœuds simulée, base jetable) :
#   1. sortie du transcodeur (sidecar : identité de la source + version) → version de la fiche ;
#      UNE ligne par média dans la liste ; version par défaut = l'original ;
#   2. job refait à l'identique (même nom) : pas de doublon ; contenu changé → la version le prend ;
#   3. fichier sans sidecar identique à une version (autre nœud) → copie de CETTE version ;
#   4. version par défaut : choix, effet sur l'envoi / le rapatriement (chemin de la bonne copie),
#      version explicite, version inconnue refusée en le disant ;
#   5. copie vers un nœud : vérifiée contre l'empreinte de la VERSION ;
#   6. supprimer une version (copies comprises), jamais l'original ; défaut ramené à l'original ;
#   7. sortie dont la source est inconnue : la fiche naît de la version ; la source analysée plus
#      tard en devient l'original (rattachée par son empreinte) ;
#   8. intégrité : empreinte comparée à celle de la version de la copie ;
#   9. projet : les versions voyagent avec la fiche ; archive : une copie par version (`@<clé>/`),
#      réimportée sous le nom codé de la version ;
#  10. macros : options de version, « Choisir la version par défaut », « Charger un média dans un
#      conteneur » (version demandée, rapatriement suivi sans copie locale) ;
#  11. script de nœud réel (script_templates/media_scan.py) : la version se lit dans le sidecar et
#      s'y écrit ; une identité orpheline est remplacée seulement quand la base le demande ;
#  12. routes HTTP déclarées (version par défaut, suppression d'une version).
#
#   $ ./venv/bin/python tests/verif_media_versions.py
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_versions_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()
from app import media_naming as mn                                      # noqa: E402
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


VOLUMES = {1: {}, 2: {}}
STAMPS = []


def fake_run(node, req, timeout=0, extra_mounts=()):
    vol = VOLUMES[node["id"]]
    if req["mode"] == "scan":
        return {"files": [dict({"path": p}, size=f["size"], mtime=f["mtime"], fp=f["fp"],
                               **{k: f[k] for k in ("id", "origin", "producer", "process", "version") if f.get(k)},
                               **({"tech": f.get("tech") or {"duration": 10.0, "video": {"width": 1920}}}
                                  if not req["known"].get(p) else {}))
                          for p, f in vol.items()], "probed": 0}
    res = {"stamped": {}, "thumbs": {}}
    for it in req.get("stamp") or []:
        STAMPS.append(dict(it, node=node["id"]))
        f = vol[it["path"]]
        if f.get("id") and f["id"] != it["id"] and not it.get("replace"):
            res["stamped"][it["path"]] = "conflict:" + f["id"]
            continue
        f["id"] = it["id"]
        if it.get("version"):
            f["version"] = it["version"]
        res["stamped"][it["path"]] = "stamped"
    for it in req.get("thumbs") or []:
        res["thumbs"][it["id"]] = "/9j/AA=="
    return res


mr._run_on_node = fake_run
mr._node = lambda nid: {"id": int(nid), "name": "n%s" % nid, "media_image": "img", "host": "h%s" % nid, "media_mount": "/vol"}
mr.proxy_auto = lambda *a, **k: None
_database.db_get_nodes = lambda: [{"id": 1, "name": "n1", "media_image": "img"}, {"id": 2, "name": "n2", "media_image": "img"}]
c = mr.db()
ids = lambda rows: sorted(a["id"] for a in rows)

A = "a1b2c3d4-0000-4000-8000-000000000001"
print("1. sortie du transcodeur = version de la fiche")
ORIG = mn.coded_name("rec/clip.mov", A)
VOLUMES[1][ORIG] = {"size": 100, "mtime": 1, "fp": "fpOrig", "id": A, "origin": "recorder"}
mr.scan_node(1)
check(mr.get_asset(A) is not None and mr.get_asset(A)["name"] == "clip.mov", "original importé (fiche « clip.mov »)")
H264 = mn.version_file_name(ORIG, A, "h264", ".mp4")
check(H264 == "rec/clip__a1b2c3_h264.mp4", "nom de la version : même code, clé en suffixe")
PROC = {"preset": "h264_standard", "version": "h264", "version_label": "H.264 Standard (crf 23)",
        "source": {"asset_id": A, "fingerprint": "fpOrig"}, "loudness": {"measured_lufs": -23.0}}
VOLUMES[1][H264] = {"size": 30, "mtime": 2, "fp": "fpH264", "id": A, "version": "h264", "origin": "transcoder",
                    "producer": "tc-1", "process": PROC, "tech": {"duration": 10.0, "video": {"width": 1280}}}
mr.scan_node(1)
a = mr.get_asset(A)
vs = {v["key"]: v for v in a["versions"]}
check(ids(mr.list_assets()) == [A], "UNE seule fiche (la liste n'a qu'une ligne)")
check(list(vs) == ["original", "h264"] and vs["h264"]["label"] == "H.264 Standard (crf 23)"
      and vs["h264"]["fingerprint"] == "fpH264" and vs["h264"]["tech"]["video"]["width"] == 1280,
      "versions : original + h264 (libellé, empreinte, fiche technique de la version)")
check(vs["original"]["default"] and a["default_version"] == "original", "version par défaut : l'original")
check(vs["h264"]["present"] == 1 and vs["h264"]["nodes"] == ["n1"], "copies et nœuds de la version")
row = mr.list_assets()[0]
check(row["versions"] == ["original", "h264"] and row["default_version"] == "original", "la ligne liste ses versions et son défaut")
check(mr.find_by_fingerprint("fpH264") == (A, "h264") and mr.find_by_fingerprint("fpOrig") == (A, "original"),
      "identification par empreinte : celle de la VERSION")

print("2. job refait à l'identique")
VOLUMES[1][H264].update(mtime=3)
mr.scan_node(1)
check(len([f for f in mr.get_asset(A)["copies"] if f["version"] == "h264"]) == 1 and ids(mr.list_assets()) == [A],
      "même nom, même contenu : ni doublon ni fiche")
VOLUMES[1][H264].update(mtime=4, fp="fpH264b", size=31, process=dict(PROC, job_id=2))
mr.scan_node(1)
v = {x["key"]: x for x in mr.get_asset(A)["versions"]}["h264"]
check(v["fingerprint"] == "fpH264b" and v["process"]["job_id"] == 2, "contenu refait par son producteur : la version le prend")

print("3. contenu identique à une version, sans sidecar, sur un autre nœud")
VOLUMES[2]["depot/copie.mp4"] = {"size": 31, "mtime": 5, "fp": "fpH264b"}
mr.scan_node(2)
f = c.execute("SELECT asset_id, version FROM media_files WHERE node_id=2 AND path='depot/copie.mp4'").fetchone()
check(f and f["asset_id"] == A and f["version"] == "h264", "rattaché à la fiche, comme copie de CETTE version")
check(any(s["path"] == "depot/copie.mp4" and s.get("version") == "h264" for s in STAMPS), "son sidecar reçoit l'identité ET la version")

print("4. version par défaut et chargement")
vues = {101: (2, "")}
mr.container_view = lambda vmid: vues[int(vmid)]
mr.link_container_project = lambda aid, vmid: None
paths, missing, origins = mr.resolve_for_container(101, [A])
check(paths == [] and missing == ["clip.mov"], "nœud 2 sans l'original : rien à charger pour la version par défaut")
paths, _m, _o = mr.resolve_for_container(101, [A], version="h264")
check(paths == ["depot/copie.mp4"], "version demandée : sa copie")
check(mr.set_default_version(A, "h264") == "h264" and mr.default_version(A) == "h264", "choisir la version par défaut")
paths, _m, origins = mr.resolve_for_container(101, [A])
check(paths == ["depot/copie.mp4"] and origins == {"depot/copie.mp4": A}, "« Charger le média » → la version par défaut")
paths, missing, _o = mr.resolve_for_container(101, [A], version="xdcam")
check(not paths and missing and "xdcam" in missing[0], "version inconnue : refusée en le disant")
try:
    mr.set_default_version(A, "xdcam")
    check(False, "défaut sur une version inexistante refusé")
except ValueError:
    check(True, "défaut sur une version inexistante refusé")
check(mr.fetch_for_container(A, 101) == "depot/copie.mp4", "rapatriement : la copie locale de la version par défaut")
mr.set_default_version(A, "original")

print("5. copie d'une version vers un nœud")
copies = []
mr._ssh_stream = lambda sh, sa, dh, da, timeout: (copies.append((sa, da)), (True, ""))[1]
import app.node_driver as _nd                                           # noqa: E402
_nd.host_exec = lambda node, cmd, input_data=None, timeout=0: (1 if cmd.startswith("test -e") else 0, "", "")
_nd.transfer_capable = lambda n: False


def arrive_on_2(sh, sa, dh, da, timeout):
    copies.append((sa, da))
    if not sa.endswith(".xml"):
        rel = da.split("/vol/", 1)[1].replace(".partial", "")
        VOLUMES[2][rel] = dict(VOLUMES[1][sa.split("/vol/", 1)[1]])
    return True, ""


mr._ssh_stream = arrive_on_2
rel = mr.copy_asset(A, 2, version="original")
r2 = c.execute("SELECT version FROM media_files WHERE node_id=2 AND path=?", (rel,)).fetchone()
check(rel == ORIG and r2 and r2["version"] == "original", "copie de l'original : même nom, version original")
VOLUMES[1][H264]["fp"] = "fpH264b"
check(mr.copy_asset(A, 2, version="h264") == "depot/copie.mp4", "version déjà présente sur le nœud : réutilisée")

print("6. supprimer une version")
mr.set_default_version(A, "h264")
gone = []
_dc = mr.delete_copy
mr.delete_copy = lambda aid, nid, path: (gone.append((nid, path)), c.execute(
    "DELETE FROM media_files WHERE node_id=? AND path=?", (nid, path)), c.commit())
try:
    mr.delete_version(A, "original")
    check(False, "l'original ne se supprime pas seul")
except ValueError:
    check(True, "l'original ne se supprime pas seul")
mr.delete_version(A, "h264")
a = mr.get_asset(A)
check(sorted(gone) == [(1, H264), (2, "depot/copie.mp4")], "ses copies supprimées (fichier + sidecar)")
check([v["key"] for v in a["versions"]] == ["original"] and a["default_version"] == "original"
      and c.execute("SELECT default_version FROM media_assets WHERE id=?", (A,)).fetchone()[0] is None,
      "version retirée, défaut ramené à l'original")
mr.delete_copy = _dc
for p in (H264,):
    VOLUMES[1].pop(p, None)
VOLUMES[2].pop("depot/copie.mp4", None)

print("7. sortie d'une source inconnue : la fiche naît de la version")
ORPH = "5e5e5e5e-0000-5000-8000-000000000009"
VOLUMES[1]["depot/itw__5e5e5e_xdcam.mxf"] = {"size": 50, "mtime": 6, "fp": "fpX", "id": ORPH, "version": "xdcam",
                                              "origin": "transcoder", "producer": "tc-1",
                                              "process": {"preset": "xdcam_hd422", "version": "xdcam",
                                                          "source": {"path": "depot/itw.mov", "fingerprint": "fpSrc",
                                                                     "size": 999, "duration": 12.0}}}
mr.scan_node(1)
o = mr.get_asset(ORPH)
check(o and o["name"] == "itw.mov" and o["fingerprint"] == "fpSrc" and [v["key"] for v in o["versions"]] == ["original", "xdcam"],
      "fiche née de la version : nom et empreinte de la source")
check(o["default_version"] == "xdcam", "original jamais vu : la version est le défaut")
VOLUMES[2]["arrivee/itw.mov"] = {"size": 999, "mtime": 7, "fp": "fpSrc"}
mr.scan_node(2)
o = mr.get_asset(ORPH)
check(any(f["path"] == "arrivee/itw.mov" and f["version"] == "original" for f in o["copies"]) and o["default_version"] == "original",
      "la source analysée plus tard en devient l'original (empreinte) et le défaut")
check(len(mr.list_assets()) == 2, "toujours une fiche par média")
# Sortie d'une source SANS identité, mais dont le contenu est connu (l'original de A) : rangée sous A,
# et son identité orpheline (tirée de l'empreinte par le transcodeur) remplacée dans son sidecar.
VOLUMES[1]["depot/clip__0f0f0f_h265.mp4"] = {"size": 20, "mtime": 6, "fp": "fpH265", "id": "0f0f0f0f-0000-5000-8000-000000000000",
                                              "version": "h265", "origin": "transcoder", "producer": "tc-1",
                                              "process": {"preset": "h265_standard", "version": "h265",
                                                          "source": {"fingerprint": "fpOrig"}}}
mr.scan_node(1)
check("h265" in [v["key"] for v in mr.get_asset(A)["versions"]] and mr.get_asset("0f0f0f0f-0000-5000-8000-000000000000") is None,
      "source connue par son contenu : la sortie rejoint sa fiche")
check(VOLUMES[1]["depot/clip__0f0f0f_h265.mp4"]["id"] == A, "identité orpheline remplacée dans le sidecar")

print("8. intégrité : empreinte de la version de la copie")
VOLUMES[1]["depot/itw__5e5e5e_xdcam.mxf"].update(fp="fpAbime", mtime=8)
c.execute("UPDATE media_versions SET process_json=NULL WHERE asset_id=?", (ORPH,))   # pas refaite par son producteur
c.commit()
VOLUMES[1]["depot/itw__5e5e5e_xdcam.mxf"].pop("process")
mr.scan_node(1)
mm = mr.integrity()["fingerprint_mismatch"]
check([(x["id"], x["version"]) for x in mm] == [(ORPH, "xdcam")], "copie abîmée d'une version signalée, l'original non")
fo = {f["path"]: f["fingerprint_ok"] for f in mr.get_asset(ORPH)["copies"]}
check(fo == {"depot/itw__5e5e5e_xdcam.mxf": False, "arrivee/itw.mov": True}, "fiche : chaque copie jugée contre SA version")
VOLUMES[1]["depot/itw__5e5e5e_xdcam.mxf"].update(fp="fpX", mtime=9)
mr.scan_node(1)

mr._ssh_stream = arrive_on_2
_nd.host_exec = lambda node, cmd, input_data=None, timeout=0: (1 if cmd.startswith("test -e") else 0, "", "")
_nd.transfer_capable = lambda n: False
rel = mr.copy_asset(ORPH, 2, version="xdcam")
r2 = c.execute("SELECT version, verified_at FROM media_files WHERE node_id=2 AND path=?", (rel,)).fetchone()
check(rel == "depot/itw__5e5e5e_xdcam.mxf" and r2["version"] == "xdcam" and r2["verified_at"],
      "copie d'une version : même nom, vérifiée contre l'empreinte de LA version")
mr.delete_copy = lambda aid, nid, path: (c.execute("DELETE FROM media_files WHERE node_id=? AND path=?", (nid, path)), c.commit())
mr.delete_copy(ORPH, 2, rel)
VOLUMES[2].pop(rel, None)
mr.delete_copy = _dc

print("9. projet : les versions voyagent")
mr.link_project(ORPH, 3)
mr.set_default_version(ORPH, "xdcam")
exp = mr.project_export_media(3)
rec = exp["media"][0]
check([v["key"] for v in rec["versions"]] == ["xdcam"] and rec["default_version"] == "xdcam", "export : versions et défaut")
c2_path = os.path.join(TMP, "autre.db")
mr.MEDIA_DB_PATH = c2_path
n = mr.project_import_media(4, json.loads(json.dumps(exp["media"])))
check(n["new"] == 1 and [v["key"] for v in mr.versions_of(ORPH)] == ["original", "xdcam"]
      and mr.find_by_fingerprint("fpX") == (ORPH, "xdcam"), "import sur un autre serveur : versions reprises (empreintes comprises)")
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
c = mr.db()
_database.db_get_project = lambda pid: {"id": pid, "media_path": ""}
_nd.transfer_capable = lambda n: True
_nd.host_exec = lambda node, cmd, timeout=0: (0, '<media id="%s">\n  <file>x</file>\n</media>' % ORPH, "") if cmd.startswith("cat ") \
    else (1, "", "")
plan = mr.project_archive_plan(3)
check(sorted(p["arc"] for p in plan) == ["media/%s/@xdcam/depot/itw.mxf" % ORPH, "media/%s/arrivee/itw.mov" % ORPH],
      "archive : une copie par version, la version sous @<clé>/, noms sans code ni clé")
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    for nm in ("media/%s/@xdcam/depot/itw.mxf" % ORPH, "media/%s/arrivee/itw.mov" % ORPH, "media/%s/@v1/x.mxf" % ORPH):
        ti = tarfile.TarInfo(nm)
        ti.size = 1
        t.addfile(ti, io.BytesIO(b"x"))
tp = os.path.join(TMP, "a.tar")
open(tp, "wb").write(buf.getvalue())
PUT = []
mr._put_on_node = lambda node, rel, f, size, mtime: PUT.append(rel)
_scan = mr.scan_node
mr.scan_node = lambda nid, **k: {}
LOCAL = "77777777-0000-4000-8000-000000000007"
mr.project_import_archive(5, tp, 2, {ORPH: LOCAL})
check(sorted(PUT) == sorted(["depot/itw__777777_xdcam.mxf", "arrivee/itw__777777.mov"]),
      "import d'archive : version sous son nom codé, clé ambiguë (@v1) écartée")
mr.scan_node = _scan

print("10. macros")
opts = mr.action_options("send_media", "version", None)
check([o["value"] for o in opts] == ["", "original", "h265", "xdcam"], "options de version : défaut, original, clés de la base")
check([o["value"] for o in mr.action_options("set_default_version", "version", None)] == ["original", "h265", "xdcam"],
      "« version par défaut » : sans l'entrée « par défaut »")
check(mr.run_action("set_default_version", {"asset": ORPH, "version": "original"}, {})["version"] == "original"
      and mr.default_version(ORPH) == "original", "action « Choisir la version par défaut »")
try:
    mr.run_action("set_default_version", {"asset": ORPH, "version": "zzz"}, {})
    check(False, "version inconnue : l'action échoue")
except RuntimeError:
    check(True, "version inconnue : l'action échoue")
sent, fetched = [], []
mr.send_to = lambda vmid, action, paths, params=None, origins=None, user="": sent.append((vmid, action, paths)) or len(paths)
mr._macro_containers = lambda pid=None: [{"value": "101", "label": "player"}]
vues[101] = (1, "")
r = mr.run_action("send_media", {"container": "101", "asset": ORPH, "version": "xdcam", "action": "load"}, {})
check(r["state"] == "sent" and sent[-1] == (101, "load", ["depot/itw__5e5e5e_xdcam.mxf"]), "charger une version précise (copie locale)")
r = mr.run_action("send_media", {"container": "101", "asset": ORPH, "action": "playlist"}, {})
check(r["state"] == "fetching" or (r["state"] == "sent" and sent[-1][2] != ["depot/itw__5e5e5e_xdcam.mxf"]),
      "sans version : la version par défaut (l'original, sur un autre nœud)")
from app import media_fetch as mf                                         # noqa: E402
_start = mf.start
mf.start = lambda aid, vmid, fetch=None, version=None: fetched.append((aid, vmid, version)) or {"state": "queued", "job": "j1"}
mf.get = lambda j: {"state": "failed", "error": "test"}
r = mr.run_action("send_media", {"container": "101", "asset": ORPH, "version": "original"}, {})
check(r["state"] == "fetching" and fetched[-1] == (ORPH, 101, "original"), "pas de copie locale : rapatriement suivi de LA version")
try:
    mr.run_action("send_media", {"container": "999", "asset": ORPH}, {"project_id": 3})
    check(False, "conteneur hors du projet de la macro refusé")
except RuntimeError:
    check(True, "conteneur hors du projet de la macro refusé")
mf.start = _start

print("11. script de nœud réel : version lue et écrite dans le sidecar")
ROOT = os.path.join(TMP, "vol")
os.makedirs(os.path.join(ROOT, "d"))
with open(os.path.join(ROOT, "d", "c__a1b2c3_h264.mp4"), "wb") as fh:
    fh.write(b"\0" * 2048)
with open(os.path.join(ROOT, "d", "c__a1b2c3_h264.mp4.xml"), "w") as fh:
    fh.write('<media id="%s" version="h264" origin="transcoder">\n</media>\n' % A)
with open(os.path.join(ROOT, "d", "o.mp4"), "wb") as fh:
    fh.write(b"\1" * 2048)
with open(os.path.join(ROOT, "d", "o.mp4.xml"), "w") as fh:
    fh.write('<media id="ORPHELIN" version="h264" origin="transcoder">\n</media>\n')
src = open(os.path.join(RACINE, "script_templates", "media_scan.py"), encoding="utf-8").read().replace('ROOT = "/m"', 'ROOT = %r' % ROOT)
scr = os.path.join(TMP, "scan.py")
open(scr, "w").write(src)


def node_script(req):
    p = subprocess.run([sys.executable, scr], input=json.dumps(req), capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout or "{}") if p.returncode == 0 else {"error": p.stderr[-300:]}


if shutil.which("ffprobe") is None:
    print("  (ffprobe absent : la fiche technique n'est pas lue, sans effet sur ce qui est vérifié)")
res = node_script({"mode": "scan", "known": {}, "probe_max": 0})
got = {f["path"]: f for f in res.get("files") or []}
check(got.get("d/c__a1b2c3_h264.mp4", {}).get("version") == "h264" and got["d/c__a1b2c3_h264.mp4"]["id"] == A,
      "relevé : identité et version du sidecar")
res = node_script({"mode": "stamp", "stamp": [{"path": "d/o.mp4", "id": A, "version": "h264"}]})
check(res.get("stamped", {}).get("d/o.mp4") == "conflict:ORPHELIN", "une autre identité est gardée sans `replace`")
res = node_script({"mode": "stamp", "stamp": [{"path": "d/o.mp4", "id": A, "version": "h264", "replace": True}]})
side = open(os.path.join(ROOT, "d", "o.mp4.xml")).read()
check(res["stamped"]["d/o.mp4"] == "stamped" and ('id="%s" version="h264"' % A) in side and 'origin="transcoder"' in side
      and "ORPHELIN" not in side, "identité orpheline remplacée (version et autres attributs gardés)")
open(os.path.join(ROOT, "d", "neuf.mp4"), "w").close()
res = node_script({"mode": "stamp", "stamp": [{"path": "d/neuf.mp4", "id": A, "version": "xdcam"}]})
check(res["stamped"]["d/neuf.mp4"] == "created" and 'version="xdcam"' in open(os.path.join(ROOT, "d", "neuf.mp4.xml")).read(),
      "sidecar créé avec la version")
res = node_script({"mode": "stamp", "stamp": [{"path": "d/neuf.mp4", "id": A, "version": "xdcam"}]})
check(res["stamped"]["d/neuf.mp4"] == "kept", "même identité, même version : gardé")

print("12. routes HTTP")
import main as app_main                                                  # noqa: E402
rules = {(r.rule, m) for r in app_main.app.url_map.iter_rules() for m in r.methods}
check(("/api/media/assets/<aid>/default_version", "POST") in rules and ("/api/media/assets/<aid>/versions/<key>", "DELETE") in rules,
      "version par défaut (POST) et suppression d'une version (DELETE) exposées")

print("\n%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
