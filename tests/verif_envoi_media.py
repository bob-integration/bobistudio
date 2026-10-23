#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : ENVOI D'UN FICHIER DEPUIS LE POSTE (par morceaux, reprenable) — agent-nœud 0.26.0 +
app/media_upload.py + routes /api/media/upload/*.

Le VRAI `Handler` de node_agent/agent.py est servi en HTTP sur la boucle locale, sur un volume
jetable ; le contrôleur lui parle par les VRAIES fonctions de node_driver (requests). Seule
l'analyse du volume (conteneur jetable sur le nœud) est simulée, par une lecture du dossier.

  Agent :
   1. un morceau s'ajoute au `.partial` au décalage = sa taille ; un autre décalage → 409 avec la
      taille RÉELLE ; l'état la rend ;
   2. un morceau coupé en route laisse un PRÉFIXE exact, d'où l'on reprend ;
   3. finalisation : taille vérifiée (409 sinon), date reposée, contenu intact ; jamais par-dessus
      un fichier existant (ni par un morceau, ni à la finalisation) ;
   4. refus : chemin hors du volume (400), espace insuffisant (507) ; abandon = `.partial` effacé ;
   5. l'envoi d'un bloc (0.25.0) marche toujours.
  Orchestrateur :
   6. session : nouvelle, puis RETROUVÉE au même fichier (nom + taille + date) — l'envoi repart de
      la taille demandée au nœud, pas de celle notée en base ;
   7. le relais lit le morceau par BLOCS (jamais d'un coup) ;
   8. fin : import dans la base (origine « upload »), projet, bin, vignette ;
   9. nom déjà pris → suffixe ; extension refusée (415) ; trop gros (413) ; annuler efface le
      `.partial` ;
  10. routes : `media.manage` exigée (403 sans), et le morceau passe par Flask jusqu'au nœud.

    $ ./venv/bin/python tests/verif_envoi_media.py
"""
import hashlib
import importlib.util
import io
import json
import os
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
    # Bases jetables AVANT tout accès (get_db épingle une connexion par thread).
    tmp = tempfile.mkdtemp(prefix="verif_envoi_")
    from app import config, database
    config.DB_PATH = database.DB_PATH = os.path.join(tmp, "t.db")
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import media_registry as mr
    mr.MEDIA_DB_PATH = os.path.join(tmp, "db_medias.db")
    mr.THUMB_DIR = os.path.join(tmp, "thumbs")
    mr.BACKUP_DIR = os.path.join(tmp, "backups")

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

    from app import node_driver as nd
    node = {"id": 1, "name": "n1", "agent_url": "http://127.0.0.1:%d" % port, "agent_token": JETON,
            "agent_version": agent.VERSION, "media_image": "img"}
    P = lambda rel: os.path.join(media, rel)                                # noqa: E731

    # ── Agent ────────────────────────────────────────────────────────────────────────────────
    print("1. morceaux au décalage")
    data = os.urandom(3 * 1024 * 1024 + 123)
    code, j = nd.upload_chunk(node, "a/clip.mp4", 0, len(data), io.BytesIO(data[:1 << 20]), 1 << 20)
    verifier(code == 200 and j.get("partial_size") == 1 << 20, "1er morceau accepté (%s %s)" % (code, j))
    code, j = nd.upload_chunk(node, "a/clip.mp4", 5, len(data), io.BytesIO(b"x" * 10), 10)
    verifier(code == 409 and j.get("partial_size") == 1 << 20, "décalage faux → 409 + taille réelle (%s %s)" % (code, j))
    code, j = nd.upload_state(node, "a/clip.mp4")
    verifier(code == 200 and j.get("partial_size") == 1 << 20 and j.get("exists") is False and j.get("free"),
             "état : taille du .partial, cible absente, espace libre")

    print("2. morceau coupé en route")
    s = socket.create_connection(("127.0.0.1", port))
    cut = data[1 << 20:(1 << 20) + 4000]
    s.sendall(("POST /v1/host/transfer/upload?dest=a/clip.mp4&offset=%d&total=%d HTTP/1.0\r\n"
               "X-Bobi-Node-Token: %s\r\nContent-Length: 100000\r\n\r\n" % (1 << 20, len(data), JETON)).encode()
              .replace(b"X-Bobi-Node-Token", nd.TOKEN_HEADER.encode()) + cut)
    s.shutdown(socket.SHUT_WR)
    s.recv(4096)
    s.close()
    real = os.path.getsize(P("a/clip.mp4.partial"))
    with open(P("a/clip.mp4.partial"), "rb") as f:
        verifier(real == (1 << 20) + 4000 and f.read() == data[:real], "préfixe exact gardé (%d octets)" % real)
    off = real
    while off < len(data):
        n = min(1 << 20, len(data) - off)
        code, j = nd.upload_chunk(node, "a/clip.mp4", off, len(data), io.BytesIO(data[off:off + n]), n)
        off = j.get("partial_size", off)
    verifier(off == len(data), "reprise jusqu'au bout")

    print("3. finalisation")
    code, j = nd.upload_finish(node, "a/clip.mp4", len(data) + 1, 1700000000)
    verifier(code == 409 and j.get("partial_size") == len(data), "taille fausse → 409")
    code, j = nd.upload_finish(node, "a/clip.mp4", len(data), 1700000000)
    with open(P("a/clip.mp4"), "rb") as f:
        same = f.read() == data
    verifier(code == 200 and same and not os.path.exists(P("a/clip.mp4.partial")), "fichier complet, .partial retiré")
    verifier(int(os.path.getmtime(P("a/clip.mp4"))) == 1700000000, "date d'origine reposée")
    code, j = nd.upload_chunk(node, "a/clip.mp4", 0, 10, io.BytesIO(b"0123456789"), 10)
    verifier(code == 409 and j.get("exists"), "morceau vers un fichier existant → 409")
    nd.upload_chunk(node, "a/autre.mp4", 0, 4, io.BytesIO(b"NEUF"), 4)
    with open(P("a/autre.mp4"), "wb") as f:
        f.write(b"ANCIEN")
    code, j = nd.upload_finish(node, "a/autre.mp4", 4)
    with open(P("a/autre.mp4"), "rb") as f:
        verifier(code == 409 and j.get("exists") and f.read() == b"ANCIEN", "finalisation sur un existant → 409, rien écrasé")

    print("4. refus et abandon")
    code, j = nd.upload_chunk(node, "../dehors.mp4", 0, 4, io.BytesIO(b"abcd"), 4)
    verifier(code == 400 and not os.path.exists(os.path.join(os.path.dirname(media), "dehors.mp4.partial")),
             "hors du volume → 400")
    marge = agent.UPLOAD_FREE_MARGIN
    agent.UPLOAD_FREE_MARGIN = 1 << 62
    gros = os.urandom(8 << 20)                  # corps plus gros que les tampons socket : le refus doit le vider
    code, j = nd.upload_chunk(node, "a/gros.mp4", 0, len(gros), io.BytesIO(gros), len(gros))
    agent.UPLOAD_FREE_MARGIN = marge
    verifier(code == 507 and j.get("no_space") and not os.path.exists(P("a/gros.mp4.partial")), "espace insuffisant → 507 (réponse lue, corps de 8 Mio)")
    code, j = nd.upload_chunk(node, "a/gros.mp4", 0, 4, io.BytesIO(b"abcd"), 4)
    verifier(code == 200, "refus lu par l'appelant malgré un corps de 8 Mio, puis envoi accepté")
    code, j = nd.upload_abort(node, "a/gros.mp4")
    verifier(code == 200 and j.get("removed") and not os.path.exists(P("a/gros.mp4.partial")), "abandon : .partial effacé")

    print("5. envoi d'un bloc (0.25.0)")
    ok, _m = nd.upload_file(node, "b/bloc.png", io.BytesIO(b"PNGDATA"), 7, mtime=1600000000)
    verifier(ok and open(P("b/bloc.png"), "rb").read() == b"PNGDATA", "toujours accepté")

    # ── Orchestrateur ────────────────────────────────────────────────────────────────────────
    def fake_run(nod, req, timeout=0, extra_mounts=()):
        if req["mode"] == "scan":
            files = []
            for d, _ds, fs in os.walk(media):
                for fn in fs:
                    if fn.lower().endswith(mr.BROWSE_EXTS):
                        full = os.path.join(d, fn)
                        rel = os.path.relpath(full, media)
                        with open(full, "rb") as fh:
                            fp = hashlib.sha1(fh.read()).hexdigest()
                        files.append({"path": rel, "size": os.path.getsize(full), "mtime": os.path.getmtime(full),
                                      "fp": fp, "tech": {"kind": "video", "duration": 4.0, "video": {"width": 64}}})
            return {"files": files, "probed": len(files)}
        return {"stamped": {it["path"]: "stamped" for it in req.get("stamp") or []},
                "thumbs": {it["id"]: "/9j/AA==" for it in req.get("thumbs") or []}}
    mr._run_on_node = fake_run
    mr._node = lambda nid: node if int(nid) == 1 else None
    database.db_get_nodes = lambda: [node]
    from app import media_upload as mu

    print("6. session et reprise")
    vid = os.urandom(5 * 1024 * 1024 + 7)
    s1 = mu.start(42, "Ma vidéo.mov", len(vid), 1650000000.5, node_id=1, folder="Envois", project_id=9)
    # Nom sur le nœud : code du média (identité réservée à l'ouverture), cf. app/media_naming.py.
    code = lambda sx: sx["asset_id"].replace("-", "")[:6]                  # noqa: E731
    D1 = "Envois/Ma vidéo__%s.mov" % code(s1)
    verifier(s1["dest"] == D1 and s1["name"] == "Ma vidéo.mov" and s1["received"] == 0 and not s1["resumed"], "nouvelle session")
    sess = mu.session_for(s1["id"], 42)
    got = mu.put_chunk(sess, 0, io.BytesIO(vid[:2 << 20]), 2 << 20)
    verifier(got == 2 << 20, "morceau relayé")
    s2 = mu.start(42, "Ma vidéo.mov", len(vid), 1650000000.5, node_id=1, folder="Envois", project_id=9)
    verifier(s2["id"] == s1["id"] and s2["resumed"] and s2["received"] == 2 << 20, "même fichier → même session, reprise à 2 Mio")
    # Le .partial a perdu des octets (disque, main humaine) : la base dit 2 Mio, le nœud 1 Mio.
    with open(P(D1 + ".partial"), "r+b") as f:
        f.truncate(1 << 20)
    s3 = mu.start(42, "Ma vidéo.mov", len(vid), 1650000000.5, node_id=1, folder="Envois", project_id=9)
    verifier(s3["received"] == 1 << 20, "reprise depuis la taille RÉELLE du nœud (%s)" % s3["received"])
    try:
        mu.put_chunk(mu.session_for(s1["id"], 42), 2 << 20, io.BytesIO(vid[2 << 20:3 << 20]), 1 << 20)
        verifier(False, "décalage périmé refusé")
    except mu.UploadError as e:
        verifier(e.code == 409 and e.extra.get("received") == 1 << 20, "décalage périmé → 409 + taille réelle")
    s4 = mu.start(42, "Ma vidéo.mov", len(vid), 1650000001, node_id=1, folder="Envois")
    verifier(s4["id"] != s1["id"] and s4["dest"] == "Envois/Ma vidéo__%s.mov" % code(s4) and s4["asset_id"] != s1["asset_id"],
             "autre date = autre fichier : autre média, autre code (pas de conflit de nom)")
    mu.cancel(mu.session_for(s4["id"], 42))
    try:
        mu.session_for(s1["id"], 7)
        verifier(False, "session d'un autre utilisateur invisible")
    except mu.UploadError as e:
        verifier(e.code == 404, "session d'un autre utilisateur invisible")

    print("7. relais en flux")

    class Compteur(io.BytesIO):
        maxi = 0

        def read(self, n=-1):
            b = super().read(n)
            Compteur.maxi = max(Compteur.maxi, len(b))
            return b
    off = 1 << 20
    while off < len(vid):
        n = min(4 << 20, len(vid) - off)
        off = mu.put_chunk(mu.session_for(s1["id"], 42), off, Compteur(vid[off:off + n]), n)
    verifier(off == len(vid) and 0 < Compteur.maxi <= 1 << 20,
             "morceau de 4 Mio lu par blocs (plus grand : %d octets)" % Compteur.maxi)

    print("8. fin et import")
    bin_id = mr.save_bin("Arrivées")
    database_proj = 9
    mu.db().execute("UPDATE media_uploads SET bin_id=? WHERE id=?", (bin_id, s1["id"]))
    mu.db().commit()
    fin = mu.finish(mu.session_for(s1["id"], 42), run_async=False)
    s = mu.public(mu.session_for(s1["id"], 42))
    verifier(fin["state"] in ("importing", "done") and s["state"] == "done" and s["asset_id"], "finalisé puis importé (%s)" % s.get("error"))
    with open(P(D1), "rb") as f:
        verifier(hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(vid).hexdigest(), "empreinte identique à l'original")
    a = mr.get_asset(s["asset_id"]) or {}
    verifier(a.get("origin") == "upload" and database_proj in (a.get("projects") or []), "origine « upload », projet 9")
    verifier(s["asset_id"] == s1["asset_id"] and a.get("name") == "Ma vidéo.mov",
             "identité RÉSERVÉE à l'ouverture = celle du média ; nom de fiche sans code")
    verifier(s["asset_id"] in [x["id"] for x in mr.bin_assets(bin_id)], "rangé dans le bin")
    verifier(os.path.exists(mr.thumb_path(s["asset_id"])), "vignette")
    verifier(int(os.path.getmtime(P(D1))) == 1650000000, "date du poste reposée")

    print("9. même nom, refus, annulation")
    s5 = mu.start(42, "Ma vidéo.mov", 10, 1, node_id=1, folder="Envois")
    verifier(s5["dest"] == "Envois/Ma vidéo__%s.mov" % code(s5) and not s5["renamed"],
             "même nom qu'un média présent → autre code, rien à renommer (%s)" % s5["dest"])
    mu.put_chunk(mu.session_for(s5["id"], 42), 0, io.BytesIO(b"0123456789"), 10)
    mu.cancel(mu.session_for(s5["id"], 42))
    verifier(not os.path.exists(P(s5["dest"] + ".partial")) and mu.session_for(s5["id"], 42)["state"] == "cancelled",
             "annuler : .partial effacé sur le nœud")
    for nom, taille, attendu in (("notes.txt", 10, 415), ("x.exe", 10, 415), ("clip.mp4", 0, 400)):
        try:
            mu.start(42, nom, taille, 1, node_id=1)
            verifier(False, "%s refusé" % nom)
        except mu.UploadError as e:
            verifier(e.code == attendu, "%s (%d o) refusé : %d" % (nom, taille, e.code))
    from app import settings as st
    st.set("media_upload_max_gb", 0.001)
    try:
        mu.start(42, "gros.mxf", 2 << 20, 1, node_id=1)
        verifier(False, "trop gros refusé")
    except mu.UploadError as e:
        verifier(e.code == 413, "plus gros que le maximum réglé → 413")
    st.set("media_upload_max_gb", 200)
    try:
        mu.start(42, "x.mp4", 10, 1, node_id=1, folder="../dehors")
        verifier(False, "dossier hors volume refusé")
    except mu.UploadError as e:
        verifier(e.code == 400, "dossier hors volume refusé")
    verifier(mu.clean_name("../../etc/.passwd.mp4") == "passwd.mp4", "nom nettoyé (chemin, caché)")

    print("10. routes")
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
    r = cli_l.post("/api/media/upload/start", json={"name": "a.mp4", "size": 4, "node_id": 1})
    verifier(r.status_code == 403, "sans media.manage → 403 (%s)" % r.status_code)
    r = cli_a.post("/api/media/upload/start", json={"name": "route.png", "size": 4, "mtime": 1, "node_id": 1, "folder": "Images"})
    j = r.get_json() or {}
    verifier(r.status_code == 200 and j.get("dest") == "Images/route__%s.png" % str(j.get("asset_id")).replace("-", "")[:6],
             "ouverture par la route (%s)" % j.get("dest"))
    r = cli_a.put("/api/media/upload/%s?offset=0" % j.get("id"), data=b"\x89PNG",
                  headers={"Content-Type": "application/octet-stream"})
    verifier(r.status_code == 200 and (r.get_json() or {}).get("received") == 4, "morceau par Flask jusqu'au nœud")
    r = cli_l.put("/api/media/upload/%s?offset=4" % j.get("id"), data=b"x")
    verifier(r.status_code == 403, "morceau sans media.manage → 403")
    r = cli_a.get("/api/media/upload/config")
    verifier(r.status_code == 200 and (r.get_json() or {}).get("nodes", [{}])[0].get("capable") is True, "config : nœud capable")
    node_old = dict(node, agent_version="0.25.0")
    verifier(not nd.upload_resumable(node_old) and nd.upload_resumable(node), "capacité par version (0.26.0)")

    print("11. interface")
    import re
    js = open(os.path.join(RACINE, "static/js/media_upload.js"), encoding="utf-8").read()
    lib = open(os.path.join(RACINE, "static/js/media_library.js"), encoding="utf-8").read()
    cles = set(re.findall(r"[TL]\('(up_[a-z_]+)'", js + lib))
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
        manque = sorted(k for k in cles if "medialib." + k not in cat)
        verifier(not manque, "%d clés medialib.up_* présentes en %s %s" % (len(cles), lang, manque or ""))
    verifier("application/x-bobi-src" in lib.split("const isFiles")[1].split("\n")[0],
             "dépôt de FICHIERS distingué d'un glisser interne (x-bobi-src)")
    verifier("'/static/js/media_upload.js'" in lib and "ml:uploaded" in lib and "ml:uploaded" in js,
             "machinerie chargée à la demande, bibliothèques relues à l'arrivée")
    verifier("beforeunload" in js and "confirm(" not in js and "prompt(" not in js,
             "alerte en quittant la page ; aucun confirm()/prompt() natif")

    httpd.shutdown()
    print("\n%s" % ("ÉCHECS : %d" % len(ECHECS) if ECHECS else "tout est vert"))
    sys.exit(1 if ECHECS else 0)


if __name__ == "__main__":
    main()
