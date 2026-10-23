#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : EMPREINTE D'UN FICHIER CALCULÉE DANS LE NAVIGATEUR (static/js/blake2b.js) = celle de la
base des médias (script_templates/media_scan.py:fingerprint), et question « déjà présent » avant
l'envoi depuis l'ordinateur (app/media_upload.py:same_content / attach_existing).

  1. BLAKE2b (RFC 7693) : vecteurs de l'annexe A (« abc », 512 bits), message vide, sorties de 1 à
     64 octets, découpage arbitraire des `update` — comparés à hashlib ;
  2. mediaFingerprint(Blob) exécuté sous node sur de VRAIS fichiers (fs.openAsBlob : lectures par
     slice) = media_scan.fingerprint, pour 0 o, 1 o, 3 Mo, 4 Mio pile, 4 Mio + 1, 6 Mo, 8 Mio pile,
     9 Mo et 300 Mo (creux, tête et queue aléatoires) ; temps mesuré ;
  3. serveur : même empreinte → copies présentes, « ne pas envoyer » proposé seulement si une copie
     est dans le dossier de destination du même nœud ; une copie absente n'est pas citée ; une
     reprise n'a pas de question ; « ne pas l'envoyer » range le média existant (projet, bin) ;
  4. page : empreinte calculée avant /check, attach au refus.

    $ ./venv/bin/python tests/verif_empreinte_navigateur.py
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def node(code, *args):
    out = subprocess.run(["node", "-e", code, *args], capture_output=True, text=True, timeout=300)
    if out.returncode:
        print(out.stderr[-2000:])
    return out.stdout


def main():
    if not shutil.which("node"):
        print("node absent : garde sautée")
        return
    b2 = os.path.join(RACINE, "static/js/blake2b.js")

    print("1. BLAKE2b (RFC 7693)")
    msgs = [b"abc", b"", bytes(range(256)) * 3, os.urandom(129), os.urandom(128), os.urandom(127), os.urandom(1000)]
    cas = []
    for i, m in enumerate(msgs):
        for n in (64, 32, 16, 1):
            cas.append({"m": m.hex(), "n": n, "split": [0, 1, 7, 128, 300][i % 5]})
    res = json.loads(node(r"""
const B = require(process.argv[1]), cas = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cas.map(c => { const u = Buffer.from(c.m, 'hex'), h = B.create(c.n);
  h.update(u.subarray(0, Math.min(c.split, u.length))); h.update(u.subarray(Math.min(c.split, u.length)));
  return B.hex(h.digest()); })));""", b2, json.dumps(cas)) or "[]")
    attendu = [hashlib.blake2b(bytes.fromhex(c["m"]), digest_size=c["n"]).hexdigest() for c in cas]
    verifier(res and res[0] == ("ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1"
                                "7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"),
             "RFC 7693 annexe A : BLAKE2b-512(« abc »)")
    verifier(res == attendu, "%d cas (vide, 127/128/129 o, 1 à 64 octets de sortie, update découpé) = hashlib" % len(cas))

    print("2. empreinte d'un fichier = media_scan.fingerprint")
    spec = importlib.util.spec_from_file_location("media_scan", os.path.join(RACINE, "script_templates/media_scan.py"))
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    tmp = tempfile.mkdtemp(prefix="verif_fp_")
    C = ms.CHUNK
    tailles = [0, 1, 3_000_000, C, C + 1, 6_000_000, 2 * C, 9_000_000, 300_000_000]
    fichiers = []
    for t in tailles:
        p = os.path.join(tmp, "f%d.bin" % t)
        with open(p, "wb") as f:
            if t <= 16 * C:
                f.write(os.urandom(t))
            else:                                           # creux : seules tête et queue comptent
                f.write(os.urandom(C + 4096))
                f.seek(t - C - 4096)
                f.write(os.urandom(C + 4096))
        fichiers.append(p)
    res = json.loads(node(r"""
const B = require(process.argv[1]), fs = require('fs');
(async () => { const out = [];
  for(const p of JSON.parse(process.argv[2])){ const blob = await fs.openAsBlob(p), t0 = performance.now();
    const fp = await B.mediaFingerprint(blob); out.push({fp, ms: performance.now() - t0}); }
  console.log(JSON.stringify(out)); })();""", b2, json.dumps(fichiers)) or "[]")
    for t, p, r in zip(tailles, fichiers, res + [{}] * (len(tailles) - len(res))):
        verifier(r.get("fp") == ms.fingerprint(p, os.path.getsize(p)),
                 "%s o : %s (%.0f ms sous node)" % (format(t, ","), r.get("fp"), r.get("ms") or -1))
    shutil.rmtree(tmp, ignore_errors=True)

    print("3. serveur : même contenu déjà dans la base")
    from app import config, database
    config.DB_PATH = database.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import media_registry as mr
    mr.MEDIA_DB_PATH = os.path.join(tempfile.mkdtemp(), "db_medias.db")
    nodes = [{"id": 1, "name": "dl360-1"}, {"id": 2, "name": "dell-1"}]
    database.db_get_nodes = lambda: nodes
    from app import media_upload as mu
    c = mr.db()
    FP = "ab" * 16
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, size) VALUES ('A1','highlight.mov','t','t','upload',?,10)", (FP,))
    for nid, path, st in ((1, "highlight.mov", "present"), (2, "Projets/X/highlight.mov", "present"), (1, "old/highlight.mov", "missing")):
        c.execute("INSERT INTO media_files(node_id, share_id, path, size, fingerprint, asset_id, kind, state) VALUES (?,0,?,10,?,'A1','main',?)",
                  (nid, path, FP, st))
    c.commit()
    d, cache = mu.same_content(FP, 1, "")
    verifier([x["path"] for x in d] == ["highlight.mov", "Projets/X/highlight.mov"] and d[0]["same_folder"] and d[0]["node"] == "dl360-1",
             "copies PRÉSENTES, celle du dossier de destination d'abord (absente non citée)")
    verifier(cache is False, "accès global : rien de caché")
    verifier(not any(x["same_folder"] for x in mu.same_content(FP, 1, "Projets/Y")[0]), "autre dossier : pas « même dossier »")
    verifier(not any(x["same_folder"] for x in mu.same_content(FP, 2, "")[0]), "autre nœud, même chemin relatif : pas « même dossier »")
    verifier(mu.same_content("cd" * 16, 1, "") == ([], False), "autre empreinte : rien")

    # check_names avec le vrai calcul de nom, nœud simulé
    mr._node = lambda nid: {"id": nid, "name": "dl360-1", "agent_version": "9.9.9", "agent_url": "http://x"}
    from app import node_driver as nd
    nd.upload_resumable = lambda n: True
    nd.upload_state = lambda n, rel: (200, {"exists": rel in ("highlight.mov",), "partial_size": 0, "free": 1 << 40})
    r = mu.check_names(42, [{"name": "highlight.mov", "size": 10, "mtime": 1, "fp": FP},
                            {"name": "autre.mov", "size": 10, "mtime": 1, "fp": FP},
                            {"name": "neuf.mov", "size": 10, "mtime": 1, "fp": "cd" * 16}], node_id=1, folder="")["items"]
    # Depuis le code du média dans le nom de fichier (2026-10-12), plus de « nom pris » : la seule
    # question qui reste est celle-ci.
    verifier(r[0]["duplicates"] and r[0]["duplicate_default"] == "skip" and not r[0]["exists"],
             "même contenu dans le dossier de destination → « ne pas l'envoyer » par défaut (pas de question de nom)")
    r2 = mu.check_names(42, [{"name": "highlight.mov", "size": 10, "mtime": 1, "fp": FP}], node_id=1, folder="Projets/Y")["items"]
    verifier(r2[0]["duplicate_default"] == "send", "autre dossier → « l'envoyer quand même » par défaut")
    verifier(not r[2].get("duplicates"), "contenu inconnu : pas de question")
    # Reprise : un envoi interrompu du même fichier n'a pas de question de doublon.
    mu.db().execute("""INSERT INTO media_uploads(id, user_id, node_id, folder, name, dest, size, mtime, state, received, created_at, updated_at)
                       VALUES ('S1', 42, 1, '', 'rush.mov', 'rush.mov', 10, 1, 'uploading', 4, 0, 0)""")
    mu.db().commit()
    r3 = mu.check_names(42, [{"name": "rush.mov", "size": 10, "mtime": 1, "fp": FP}], node_id=1, folder="")["items"][0]
    verifier(r3["resume"] and not r3.get("duplicates"), "reprise prioritaire : pas de question de doublon")
    bin_id = mr.save_bin("Arrivées")
    a = mu.attach_existing("A1", project_id=9, bin_id=bin_id)
    verifier(a["project_id"] == 9 and "A1" in [x["id"] for x in mr.bin_assets(bin_id)]
             and 9 in ((mr.get_asset("A1") or {}).get("projects") or []), "« ne pas l'envoyer » : média existant rangé (projet, bin)")
    try:
        mu.attach_existing("ZZ")
        verifier(False, "média inconnu refusé")
    except mu.UploadError as e:
        verifier(e.code == 404, "média inconnu refusé")

    print("3 bis. portée de l'utilisateur (projets)")
    # A1 est rattaché au projet 9 (ci-dessus). Un utilisateur des projets {5} ne le voit pas.
    d5, cache5 = mu.same_content(FP, 1, "", scope={5})
    verifier(d5 == [] and cache5 is True, "hors de ses projets : ni nœud ni chemin, seulement « existe »")
    d9, cache9 = mu.same_content(FP, 1, "", scope={9})
    verifier(len(d9) == 2 and cache9 is False, "dans ses projets : copies décrites")
    rr = mu.check_names(42, [{"name": "highlight.mov", "size": 10, "mtime": 1, "fp": FP}], node_id=1, folder="", scope={5})["items"][0]
    verifier(rr["duplicates"] == [] and rr["duplicate_hidden"] and rr["duplicate_default"] == "send"
             and "highlight.mov" not in json.dumps(rr.get("duplicates")) and "dl360" not in json.dumps(rr),
             "check hors portée : « existe déjà », « l'envoyer quand même » par défaut, aucun chemin")
    for kw, code, lib in (({"scope": {5}}, 404, "média hors portée → 404 (existence non confirmée)"),
                          ({"scope": {9}, "project_id": 5}, 403, "projet hors portée → 403")):
        try:
            mu.attach_existing("A1", **kw)
            verifier(False, lib)
        except mu.UploadError as e:
            verifier(e.code == code, lib)
    autre_bin = mr.save_bin("Bin du 5", project_id=5)
    try:
        mu.attach_existing("A1", scope={9}, bin_id=autre_bin)
        verifier(False, "bin d'un autre projet → 403")
    except mu.UploadError as e:
        verifier(e.code == 403 and "A1" not in [x["id"] for x in mr.bin_assets(autre_bin)], "bin d'un autre projet → 403, rien rangé")
    ok9 = mu.attach_existing("A1", scope={9}, project_id=9, bin_id=bin_id)
    verifier(ok9["project_id"] == 9, "dans sa portée : accepté")

    print("3 ter. routes : la portée de l'utilisateur connecté est appliquée")
    from app.auth import hash_password
    database.db_create_user("banc", hash_password("Banc-Motdepasse-2026!"), "admin")
    import main as app_main
    import app.routes.media_upload_api as rapi
    flask_app = app_main.app
    flask_app.config["TESTING"] = True
    cli = flask_app.test_client()
    uid = database.get_db().execute("SELECT id FROM users WHERE username='banc'").fetchone()["id"]
    with cli.session_transaction() as ss:
        ss["user_id"], ss["username"] = uid, "banc"
    # /check et /attach lisent la portée par ms.user_scope() depuis a4b0873 (projets + partagés) :
    # c'est elle qu'on bouche, scoped_project_ids ne sert plus qu'aux autres routes.
    from app.media_sharing import MediaScope
    _vrai_scope = rapi.ms.user_scope
    rapi.scoped_project_ids = lambda: {5}
    rapi.ms.user_scope = lambda user=None: MediaScope({5})   # utilisateur limité au projet 5
    j = cli.post("/api/media/upload/check", json={"node_id": 1, "folder": "", "items": [{"name": "x.mov", "size": 10, "fp": FP}]}).get_json() or {}
    it = (j.get("items") or [{}])[0]
    verifier(it.get("duplicate_hidden") and it.get("duplicates") == [] and "highlight" not in json.dumps(j),
             "route /check : copies hors portée cachées")
    r = cli.post("/api/media/upload/attach", json={"asset_id": "A1", "project_id": 5})
    verifier(r.status_code == 404, "route /attach : média hors portée → 404 (%s)" % r.status_code)
    rapi.scoped_project_ids = lambda: {9}
    rapi.ms.user_scope = lambda user=None: MediaScope({9})
    r = cli.post("/api/media/upload/attach", json={"asset_id": "A1", "project_id": 5})
    verifier(r.status_code == 403, "route /attach : projet hors portée → 403 (%s)" % r.status_code)
    rapi.ms.user_scope = _vrai_scope

    print("4. page")
    js = open(os.path.join(RACINE, "static/js/media_upload.js"), encoding="utf-8").read()
    rn = js.split("async function resolveNames")[1].split("function askDuplicate")[0]
    verifier(rn.index("fingerprints(files)") < rn.index("/api/media/upload/check") and "fp:fps[i]" in rn,
             "empreinte calculée AVANT la vérification, envoyée au serveur")
    verifier("/api/media/upload/attach" in rn and "!x.resume" in rn and "duplicate_hidden" in rn,
             "refus → média existant rangé ; reprise sans question ; contenu hors portée questionné aussi")
    verifier("duplicate_default === 'skip'" in js, "bouton par défaut selon le dossier de destination")
    print("\n%s" % ("ÉCHECS : %d" % len(ECHECS) if ECHECS else "tout est vert"))
    sys.exit(1 if ECHECS else 0)


if __name__ == "__main__":
    main()
