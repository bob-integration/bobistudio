#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : un média PARTAGÉ avec l'utilisateur sert aussi à l'ENVOI (pré-contrôle de doublon, « ne pas
l'envoyer »), à l'ÉDITEUR D'IMAGE et à la FERME de transcodage (2026-10-16). Une seule fonction de
portée : media_sharing.user_scope() — aucune route n'en recalcule une autre (`scoped_project_ids`).

Base jetable. `own` (P2) partage B (P2) avec `restr` (P1, restreint). `out` (P3) n'a rien.
  1. envoi : sans partage le doublon est caché (existence seule) ; partagé « use » → il est décrit ;
     « ne pas l'envoyer » sur un média seulement partagé ne le RATTACHE PAS au projet (il sortirait du partage) ;
  2. éditeur : info/enregistrer admis en « use » (vers une NOUVELLE fiche, rangée chez l'éditeur) ;
     la version d'un média partagé exige « edit » (sinon : fiche à part) ; écraser une retouche-version aussi ;
  3. ferme : un job SUR un média partagé exige « edit » (la sortie devient une version de la fiche),
     « use » → 403 ; le job reste visible de son auteur seul ;
  4. interface : « Partager… » masqué sans `media.manage` (hasPerm exposé par layout.html).

    $ ./venv/bin/python tests/verif_media_partage_usages.py
"""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_partage_usages_")
import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(TMP, "principale.db")                     # jamais la base de prod
database.init_db()
from app import media_registry as mr                                      # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")
from app import media_edit as me                                          # noqa: E402
from app import media_upload as mu                                        # noqa: E402
from app import transcode_farm as farm                                    # noqa: E402
from app import settings as _st                                           # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    from app import auth
    from app.auth import hash_password
    P1 = database.db_import_project("Régie A", [])
    P2 = database.db_import_project("Régie B", [])
    P3 = database.db_import_project("Régie C", [])
    pw = hash_password("Banc-Motdepasse-2026!")
    for u in ("own", "restr", "out"):
        database.db_create_user(u, pw, "exploitant")
    uid = {u: database.get_db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()["id"]
           for u in ("own", "restr", "out")}
    database.db_set_project_member(P2, uid["own"], "owner")
    database.db_set_project_member(P1, uid["restr"], "owner")
    database.db_set_project_member(P3, uid["out"], "owner")

    c = mr.db()
    now = "2026-10-16T00:00:00"
    FP = "ab" * 16
    B = "B000000"
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, tech_json, meta_json) VALUES (?,?,?,?,?,?,?,?)",
              (B, "b.png", now, now, "import", FP, json.dumps({"kind": "image", "video": {"width": 10, "height": 10, "codec": "png"}}), "{}"))
    c.execute("INSERT INTO media_files(node_id, path, size, asset_id, kind, state) VALUES (1,?,10,?,'main','present')", ("pB/b.png", B))
    c.execute("INSERT INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)", (P2, B, now))
    c.commit()

    import main as app_main
    flask_app = app_main.app
    flask_app.config["TESTING"] = True
    cli = flask_app.test_client()
    auth.ROLES["exploitant"] |= {"media.manage", "plugins.operate"}

    def as_(who):
        with cli.session_transaction() as ss:
            ss.clear()
            ss["user_id"], ss["username"] = uid[who], who
            row = database.get_db().execute("SELECT session_epoch FROM users WHERE id=?", (uid[who],)).fetchone()
            ss["session_epoch"] = (row["session_epoch"] if row else 0) or 0

    def code(r):
        return r.status_code, (r.get_json(silent=True) or {}).get("reason")

    def share(level):
        as_("own")
        r = cli.post("/api/media/shares", json={"targets": [{"kind": "asset", "id": B}],
                                                "recipients": [{"kind": "user", "id": uid["restr"]}], "level": level})
        assert r.status_code == 200, r.get_json()

    # Doubles : la copie est « sur le nœud 1 », sans nœud réel.
    mu._resolve_target = lambda node_id, folder, vmid: ({"id": 1, "name": "n1"}, "")
    saves = []
    me.start_save = lambda aid, recipe, overwrite=None, as_version=False, link_projects=None: \
        saves.append((aid, overwrite, as_version, link_projects)) or "job1"

    print("1. envoi depuis le poste")
    as_("restr")
    chk = lambda: cli.post("/api/media/upload/check", json={"items": [{"name": "b.png", "size": 10, "fp": FP}], "node_id": 1}).get_json()
    r = chk()
    it = (r.get("items") or [{}])[0]
    verifier(not it.get("duplicates") and it.get("duplicate_hidden") is True, "sans partage : doublon caché (existence seule)")
    verifier(code(cli.post("/api/media/upload/attach", json={"asset_id": B, "project_id": P1})) == (404, None),
             "sans partage : « ne pas l'envoyer » → média inconnu")
    share("use")
    as_("restr")
    it = (chk().get("items") or [{}])[0]
    verifier([d["asset_id"] for d in it.get("duplicates") or []] == [B] and not it.get("duplicate_hidden"),
             "partagé : le doublon est décrit (pré-contrôle)")
    r = cli.post("/api/media/upload/attach", json={"asset_id": B, "project_id": P1})
    verifier(r.status_code == 200 and r.get_json().get("shared_only") is True and r.get_json().get("project_id") is None,
             "« ne pas l'envoyer » : accepté, signalé shared_only")
    verifier(c.execute("SELECT 1 FROM project_media WHERE asset_id=? AND project_id=?", (B, P1)).fetchone() is None,
             "… et B n'est PAS rattaché à P1 (il resterait un partage, pas un bien du projet)")
    as_("out")
    verifier(code(cli.post("/api/media/upload/attach", json={"asset_id": B, "project_id": P3})) == (404, None),
             "un tiers sans partage : toujours 404")

    print("2. éditeur d'image")
    as_("restr")
    verifier(cli.get("/api/media/edit/%s/info" % B).status_code in (200, 400), "info : plus de 404 (400 = pas une image décodable ici)")
    verifier(code(cli.get("/api/media/edit/%s/info" % B))[0] != 404, "info d'un média partagé « use » : visible")
    rcp = {"out": {"w": 100, "h": 100}, "view": {"x": 0.5, "y": 0.5, "k": 1, "rot": 0}, "bg": {"mode": "color", "color": "#000000"}}
    saves.clear()
    r = cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp})
    verifier(r.status_code == 200 and saves and saves[0][0] == B and saves[0][2] is False and saves[0][3] == [P1],
             "enregistrer (« use », réglage par défaut) : NOUVELLE fiche, rangée dans les projets de l'éditeur (P1), pas ceux de l'original")
    _st.set("media_edit_output", "version")
    saves.clear()
    r = cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp})
    verifier(r.status_code == 200 and saves and saves[0][2] is False,
             "réglage « version » mais niveau « use » : repli sur une fiche à part")
    verifier(code(cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp, "overwrite": "@edit", "confirm_overwrite": True}))
             == (403, "shared_read_only"), "écraser une retouche-version d'un média « use » → 403")
    share("edit")
    as_("restr")
    saves.clear()
    r = cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp})
    verifier(r.status_code == 200 and saves and saves[0][2] is True, "niveau « edit » + réglage « version » : rangée comme VERSION")
    _st.set("media_edit_output", "asset")
    saves.clear()
    cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp})
    verifier(saves and saves[0][2] is False, "réglage « fiche à part » : jamais de version")
    as_("own")
    saves.clear()
    cli.post("/api/media/edit/%s/save" % B, json={"recipe": rcp})
    verifier(saves and saves[0][3] is None, "le propriétaire : projets de l'originale (comportement inchangé)")
    as_("out")
    verifier(code(cli.get("/api/media/edit/%s/info" % B))[0] == 404, "un tiers sans partage : 404")

    print("3. ferme de transcodage")
    farm.ensure_tables()
    farm.asset_source = lambda aid, version=None: (1, "pB/b.png")
    share("use")
    as_("restr")
    sub = {"source": {"kind": "asset", "asset_id": B}, "type": "video", "params": {"preset": "h264"}}
    verifier(code(cli.post("/api/transcode-farm/jobs", json=sub)) == (403, "shared_read_only"), "job sur média « use » → 403")
    share("edit")
    as_("restr")
    r = cli.post("/api/transcode-farm/jobs", json=sub)
    jid = (r.get_json() or {}).get("id")
    verifier(r.status_code == 200 and jid, "job sur média « edit » → créé (%s)" % r.get_json())
    row = farm.get_job(jid) or {}
    verifier(row.get("project_id") is None and row.get("src_asset") == B and row.get("src_node_id") == 1
             and row.get("output_dir") == "pB", "job hors projet, lié au média, sortie à côté de la source")
    verifier(cli.get("/api/transcode-farm/jobs/%d" % jid).status_code == 200, "l'auteur voit son job")
    verifier(jid in [j["id"] for j in cli.get("/api/transcode-farm/jobs").get_json()["jobs"]], "… dans la liste")
    as_("out")
    verifier(cli.get("/api/transcode-farm/jobs/%d" % jid).status_code == 404, "un tiers : job introuvable")
    verifier(jid not in [j["id"] for j in cli.get("/api/transcode-farm/jobs").get_json()["jobs"]], "… et absent de sa liste")
    as_("restr")
    c.execute("DELETE FROM media_shares")
    c.commit()
    verifier(cli.get("/api/transcode-farm/jobs/%d" % jid).status_code == 404, "partage retiré : le job n'est plus visible")
    verifier(code(cli.post("/api/transcode-farm/jobs", json={"source": {"kind": "asset", "asset_id": "inconnu"}, "type": "video"}))[0] == 404,
             "média inconnu → 404")

    print("4. interface et code")
    src_up = open(os.path.join(RACINE, "app/routes/media_upload_api.py"), encoding="utf-8").read()
    src_ed = open(os.path.join(RACINE, "app/routes/media_edit_api.py"), encoding="utf-8").read()
    src_fa = open(os.path.join(RACINE, "app/routes/transcode_farm_api.py"), encoding="utf-8").read()
    verifier("scope=scoped_project_ids()" not in src_up and src_up.count("ms.user_scope()") == 2,
             "envoi : pré-contrôles sur user_scope()")
    verifier("scoped_project_ids()" not in src_ed, "éditeur : plus de portée « projets seuls »")
    verifier("ms.user_scope()" in src_fa, "ferme : portée étendue par user_scope()")
    js = open(os.path.join(RACINE, "static/js/media_library.js"), encoding="utf-8").read()
    lay = open(os.path.join(RACINE, "templates/layout.html"), encoding="utf-8").read()
    verifier("'media.manage'] %}" in lay.replace(",'media.manage']", ",'media.manage']"), "layout : media.manage exposé à hasPerm")
    verifier(js.count("canShare()") >= 5 and "window.hasPerm('media.manage')" in js,
             "« Partager… » conditionné par canShare() (menu média, sélection, bin, fiche Infos)")
    verifier("openShare([{kind:'asset'" in js and "if(canShare()) out.push({label:L('sh_share'" in js
             and "if(canShare()) items.push({label:L('sh_share'" in js and "if(own.length && canShare())" in js,
             "aucune entrée « Partager… » sans garde")

    print("\n%s" % ("ÉCHECS : %d" % len(ECHECS) if ECHECS else "tout est vert"))
    sys.exit(1 if ECHECS else 0)


if __name__ == "__main__":
    main()
