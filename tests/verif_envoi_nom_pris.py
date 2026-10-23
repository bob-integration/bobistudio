#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : ENVOI DEPUIS L'ORDINATEUR — nom de fichier dans le dossier de destination
(app/media_upload.py:check_names / validate_rename, route /api/media/upload/check, page
static/js/media_upload.js).

Le VRAI agent-nœud est servi sur la boucle locale, sur un volume jetable (comme
tests/verif_envoi_media.py).

  DEPUIS LE 2026-10-12 (code du média dans le nom de fichier, app/media_naming.py) : le fichier
  s'appelle `<nom>__<code>.<ext>` — deux médias ne se disputent jamais un nom, la question « nom déjà
  pris » ne se pose plus (seule reste « ce contenu est déjà là », tests/verif_empreinte_navigateur.py).
  1. nom libre → ok ; fichier présent sous le même nom (sans code) → ok quand même ;
  2. deux fichiers de la même sélection au même nom → ok tous deux ; un nom visé par un envoi en
     cours (autre utilisateur) → ok ;
  3. nom saisi revérifié : caractère interdit, autre extension, nom caché refusés ; sans extension →
     celle d'origine ajoutée ; libre → accepté ;
  4. ouverture : fichier `<nom>__<code>.<ext>`, identité réservée ; le fichier homonyme sans code
     n'est pas touché ; nom codé lui-même pris → code à 8, rien n'est écrasé ;
  5. reprise PRIORITAIRE : un envoi interrompu du même fichier n'est pas une collision, même renommé
     (retrouvé par son nom sur le poste) ;
  6. route : media.manage exigée ; la page appelle /check avant /start, sans prompt()/confirm().

    $ ./venv/bin/python tests/verif_envoi_nom_pris.py
"""
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
JETON = "jeton-de-banc"
ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def port_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="verif_nom_pris_")
    from app import config, database
    config.DB_PATH = database.DB_PATH = os.path.join(tmp, "t.db")
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import media_registry as mr
    mr.MEDIA_DB_PATH = os.path.join(tmp, "db_medias.db")
    mr.THUMB_DIR = os.path.join(tmp, "thumbs")

    media = tempfile.mkdtemp(prefix="volume-")
    cfgdir = tempfile.mkdtemp()
    with open(os.path.join(cfgdir, "config.json"), "w") as f:
        json.dump({"token": JETON, "media_mount": media, "capabilities": ["media"]}, f)
    os.environ["BOBI_NODE_AGENT_CONFIG"] = os.path.join(cfgdir, "config.json")
    spec = importlib.util.spec_from_file_location("bobi_node_agent", os.path.join(RACINE, "node_agent", "agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    agent.CONFIG["media_mount"] = media
    port = port_libre()
    httpd = agent.ThreadingHTTPServer(("127.0.0.1", port), agent.Handler)
    httpd.handle_error = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    node = {"id": 1, "name": "n1", "agent_url": "http://127.0.0.1:%d" % port, "agent_token": JETON,
            "agent_version": agent.VERSION, "media_image": "img"}
    P = lambda rel: os.path.join(media, rel)                                # noqa: E731
    mr._node = lambda nid: node if int(nid) == 1 else None
    database.db_get_nodes = lambda: [node]
    from app import media_upload as mu
    os.makedirs(P("Envois"))
    with open(P("Envois/highlight.mov"), "wb") as f:
        f.write(b"EXISTANT")

    def chk(items, **kw):
        return mu.check_names(42, items, node_id=1, folder="Envois", **kw)["items"]

    print("1. nom libre, nom pris")
    r = chk([{"name": "neuf.mov", "size": 10, "mtime": 1}, {"name": "highlight.mov", "size": 10, "mtime": 1}])
    verifier(r[0]["ok"] and not r[0]["exists"] and r[0]["final"] == "neuf.mov", "nom libre → ok")
    verifier(r[1]["ok"] and not r[1]["exists"] and r[1]["suggestion"] is None,
             "fichier présent sous ce nom → ok (le fichier envoyé portera son code) (%s)" % r[1])
    verifier(mu.check_names(42, [{"name": "highlight.mov", "size": 1}], node_id=1, folder="")["items"][0]["ok"],
             "le même nom dans un AUTRE dossier est libre")

    print("2. sélection et envois en cours")
    r = chk([{"name": "highlight.mov", "size": 10}, {"name": "highlight.mov", "size": 11}, {"name": "x.mp4", "size": 3},
             {"name": "x.mp4", "size": 4}])
    verifier(all(x["ok"] and not x["exists"] for x in r), "même nom deux fois dans la sélection → ok (deux médias, deux codes)")
    autre = mu.start(7, "rush.mxf", 100, 5, node_id=1, folder="Envois")
    r = chk([{"name": "rush.mxf", "size": 100, "mtime": 5}])
    verifier(r[0]["ok"] and not r[0]["exists"], "nom visé par l'envoi en cours d'un autre utilisateur → ok")
    verifier(autre["dest"] == "Envois/rush__%s.mxf" % autre["asset_id"].replace("-", "")[:6], "fichier codé (%s)" % autre["dest"])

    print("3. nom saisi revérifié")
    def ren(new, reserved=()):
        return chk([{"name": "highlight.mov", "size": 10, "new_name": new}], reserved=reserved)[0]
    for new, motif in (("a/b.mov", "interdit"), ('clip?.mov', "interdit"), ("clip.mp4", "extension"), (".cache.mov", "point"),
                       ("   ", "vide")):
        x = ren(new)
        verifier(x["error"] and motif in x["error"] and not x["ok"], "« %s » refusé (%s)" % (new, x["error"]))
    x = ren("Résumé match")
    verifier(x["ok"] and x["final"] == "Résumé match.mov", "sans extension → celle d'origine ajoutée (%s)" % x["final"])
    x = ren("prise v1.2")
    verifier(x["ok"] and x["final"] == "prise v1.2.mov", "« .2 » n'est pas une extension → .mov ajouté")
    x = ren("HIGHLIGHT_final.MOV")
    verifier(x["ok"] and x["final"] == "HIGHLIGHT_final.MOV", "extension d'origine en majuscules acceptée")
    x = ren("autre.mov", reserved=["autre.mov"])
    verifier(x["ok"] and not x["exists"], "`reserved` des anciennes pages : sans effet")
    try:
        mu.validate_rename("ok.mov", "highlight.mov")
        verifier(True, "validate_rename direct")
    except mu.UploadError:
        verifier(False, "validate_rename direct")

    print("4. ouverture : nom codé, rien d'écrasé")
    with open(P("Envois/course.mov"), "wb") as f:
        f.write(b"ARRIVE")
    s = mu.start(42, "course.mov", 4, 1, node_id=1, folder="Envois")
    code = s["asset_id"].replace("-", "")[:6]
    verifier(s["dest"] == "Envois/course__%s.mov" % code and not s["renamed"] and s["name"] == "course.mov",
             "fichier « course__<code>.mov », nom voulu gardé (%s)" % s["dest"])
    with open(P("Envois/course.mov"), "rb") as f:
        verifier(f.read() == b"ARRIVE", "le fichier homonyme sans code n'est pas touché")
    mu.cancel(mu.session_for(s["id"], 42))
    # Nom codé déjà occupé (fichier déposé à la main sous ce nom) : code à 8, signalé.
    import uuid as _u
    fixe = str(_u.uuid4())
    _u4 = _u.uuid4
    _u.uuid4 = lambda: _u.UUID(fixe)
    with open(P("Envois/pris__%s.mov" % fixe.replace("-", "")[:6]), "wb") as f:
        f.write(b"A LA MAIN")
    s = mu.start(42, "pris.mov", 4, 1, node_id=1, folder="Envois")
    _u.uuid4 = _u4
    verifier(s["dest"] == "Envois/pris__%s.mov" % fixe.replace("-", "")[:8] and s["renamed"],
             "nom codé occupé → code à 8, rien d'écrasé (%s)" % s["dest"])
    mu.cancel(mu.session_for(s["id"], 42))

    print("5. reprise prioritaire")
    s = mu.start(42, "Interview.mov", 2048, 77, node_id=1, folder="Envois")
    mu.put_chunk(mu.session_for(s["id"], 42), 0, io.BytesIO(b"z" * 1024), 1024)
    r = chk([{"name": "Interview.mov", "size": 2048, "mtime": 77}])
    verifier(r[0]["ok"] and r[0]["resume"] and not r[0]["exists"] and r[0]["final"] == "Interview.mov",
             "même fichier interrompu → reprise, pas de question (%s)" % r[0])
    r = chk([{"name": "Interview.mov", "size": 2049, "mtime": 77}])
    verifier(r[0]["ok"] and not r[0]["resume"], "autre taille = autre fichier → pas de reprise")
    # Renommé à l'envoi (page d'avant la règle) puis interrompu : re-choisi sous son nom du poste.
    s2 = mu.start(42, "highlight_2.mov", 4096, 88, node_id=1, folder="Envois", source_name="highlight.mov")
    mu.put_chunk(mu.session_for(s2["id"], 42), 0, io.BytesIO(b"y" * 1000), 1000)
    r = chk([{"name": "highlight.mov", "size": 4096, "mtime": 88}])
    verifier(r[0]["resume"] and r[0]["final"] == "highlight_2.mov" and r[0].get("received") == 1000,
             "envoi RENOMMÉ retrouvé par son nom du poste → reprise sous le nom choisi")
    s3 = mu.start(42, "highlight.mov", 4096, 88, node_id=1, folder="Envois", source_name="highlight.mov")
    verifier(s3["id"] == s2["id"] and s3["resumed"] and s3["received"] == 1000,
             "ouverture sous le nom du poste → même session, reprise à 1000 octets")
    verifier(mu.public(mu.session_for(s2["id"], 42)).get("source_name") == "highlight.mov", "nom du poste gardé en session")

    print("6. route et page")
    from app.auth import hash_password
    database.db_create_user("banc", hash_password("Banc-Motdepasse-2026!"), "admin")
    database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True
    users = {r["username"]: r["id"] for r in database.get_db().execute("SELECT id, username FROM users")}
    cli_l, cli_a = app.test_client(), app.test_client()
    for cli, nom in ((cli_l, "lecteur"), (cli_a, "banc")):
        with cli.session_transaction() as ss:
            ss["user_id"], ss["username"] = users[nom], nom
    body = {"items": [{"name": "highlight.mov", "size": 3}], "node_id": 1, "folder": "Envois"}
    verifier(cli_l.post("/api/media/upload/check", json=body).status_code == 403, "sans media.manage → 403")
    rr = cli_a.post("/api/media/upload/check", json=body)
    j = rr.get_json() or {}
    verifier(rr.status_code == 200 and j.get("items", [{}])[0].get("ok") and j.get("folder") == "Envois",
             "route : nom accepté, pas de question de nom (%s %s)" % (rr.status_code, j))
    verifier(cli_a.post("/api/media/upload/check", json={"items": [], "node_id": 1}).status_code == 400, "liste vide → 400")
    js = open(os.path.join(RACINE, "static/js/media_upload.js"), encoding="utf-8").read()
    verifier("confirm(" not in js and "prompt(" not in js, "aucun confirm()/prompt() natif")
    intake = js.split("async function intake")[1].split("async function resolveNames")[0]
    verifier("resolveNames(" in intake and "queue.push" in intake.split("resolveNames(")[1],
             "la sélection est vérifiée AVANT d'entrer dans la file d'envoi")
    verifier("source_name:f.name" in js and re.search(r"name:it\.name \|\| f\.name", js), "start reçoit le nom choisi et celui du poste")
    httpd.shutdown()
    print("\n%s" % ("ÉCHECS : %d" % len(ECHECS) if ECHECS else "tout est vert"))
    sys.exit(1 if ECHECS else 0)


if __name__ == "__main__":
    main()
