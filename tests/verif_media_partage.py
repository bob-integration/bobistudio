#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : PARTAGE D'UN MÉDIA OU D'UN BIN avec un utilisateur, un groupe ou un projet (2026-10-12,
app/media_sharing.py — « cela permet de donner l'accès à des médias hors projets »).

Base jetable. Projets P1, P2, P3. Utilisateurs : `adm` (accès global), `own` (restreint, membre de P2 :
le PARTAGEUR), `restr` (restreint, membre de P1), `gm` (restreint, sans projet, membre du groupe G),
`out` (restreint, membre de P3). Médias : A (P1), B (P2), C (sans projet), D (P2), E (P2, dans un
sous-bin), F (P3, rangé dans le bin de P2 : le partageur ne le voit pas), S (P2, « smart… »),
X (P3, « smart… »). Bins : BB (P2) ⊃ SUB (P2) et SUBX (P3, invisible du partageur) ; SMART
(global, recherche « smart »).

  1. sans partage : chacun sa portée ; 2. média partagé à un UTILISATEUR (« use ») : liste, fiche,
  vignette, planche… visibles ; actions de fiche, suppression, rattachement, renommage → 403 ;
  3. niveau « edit » : la fiche se modifie, la suppression reste refusée ; 4. bin partagé à un
  GROUPE : bin VIVANT (sous-bins, entrées ultérieures), borné à la portée du PARTAGEUR (F et SUBX
  jamais exposés), « Partagés avec moi », marquage ; ranger : refusé en « use », admis en « edit » ;
  5. bin INTELLIGENT partagé à un PROJET : seulement ce que le partageur verrait (S, pas X) ;
  6. médias sans projet partagés par l'admin ; 7. qui partage : pas un destinataire (repartage),
  pas hors périmètre, pas sans `media.manage` ; 8. date de fin (passée refusée, échue → retirée et
  journalisée) ; retrait immédiat ; journal ; groupe quitté / partageur supprimé → plus rien ;
  9. charger un média partagé dans un conteneur : chargé, PAS rattaché au projet du conteneur ;
  10. accès global inchangé ; 11. groupes (API réservée à `settings.edit`) ; 12. macros.

    $ ./venv/bin/python tests/verif_media_partage.py
"""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_partage_")
import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(TMP, "principale.db")                     # jamais la base de prod
database.init_db()
from app import media_registry as mr                                      # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")
from app import media_storyboard as sbm                                   # noqa: E402
sbm.STORYBOARD_DIR = os.path.join(TMP, "storyboards")
from app import media_sharing as ms                                       # noqa: E402
from app import user_groups as ug                                         # noqa: E402

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
    database.db_create_user("adm", pw, "admin")
    for u in ("own", "restr", "gm", "out"):
        database.db_create_user(u, pw, "exploitant")
    uid = {u: database.get_db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()["id"]
           for u in ("adm", "own", "restr", "gm", "out")}
    database.db_set_project_member(P2, uid["own"], "owner")
    database.db_set_project_member(P1, uid["restr"], "owner")
    database.db_set_project_member(P3, uid["out"], "owner")
    G = ug.create_group("Prestataires")
    ug.set_members(G, [uid["gm"]])

    c = mr.db()
    now = "2026-10-12T00:00:00"
    os.makedirs(mr.THUMB_DIR, exist_ok=True)
    os.makedirs(sbm.STORYBOARD_DIR, exist_ok=True)
    spec = {"A": ("a.mov", P1), "B": ("b.mov", P2), "C": ("c.mov", None), "D": ("d.mov", P2), "E": ("e.mov", P2),
            "F": ("f.mov", P3), "S": ("smart_s.mov", P2), "X": ("smart_x.mov", P3), "D2": ("d2.mov", P2)}
    for k, (name, pid) in spec.items():
        aid = k + "000000"
        c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, tech_json, meta_json) VALUES (?,?,?,?,?,?,?)",
                  (aid, name, now, now, "import", json.dumps({"kind": "image", "video": {"width": 10, "height": 10, "codec": "png"}}), "{}"))
        c.execute("INSERT INTO media_files(node_id, path, size, asset_id, kind, state) VALUES (1,?,10,?,'main','present')",
                  (("p%s/" % pid if pid else "") + name, aid))
        if pid:
            c.execute("INSERT INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)", (pid, aid, now))
        open(mr.thumb_path(aid), "wb").write(b"\xff\xd8jpeg")
        jp, js = sbm.paths(aid)
        open(jp, "wb").write(b"\xff\xd8jpeg")
        json.dump({"n": 1, "cols": 1, "rows": 1, "w": 10, "h": 10, "times": [0]}, open(js, "w"))
    c.commit()
    A, B, C, D, E, F, S, X, D2 = (k + "000000" for k in ("A", "B", "C", "D", "E", "F", "S", "X", "D2"))
    BB = mr.save_bin("Rushes B", project_id=P2)
    mr.bin_add(BB, [D, F])
    SUB = mr.save_bin("Sous-bin B", parent_id=BB, project_id=P2)
    mr.bin_add(SUB, [E])
    SUBX = mr.save_bin("Sous-bin de P3", parent_id=BB, project_id=P3)
    mr.bin_add(SUBX, [X])
    SMART = mr.save_bin("Intelligent", query={"q": "smart"})

    calls = []
    mr.container_view = lambda vmid: (1, "")
    mr.container_context = lambda vmid: {"vmid": vmid, "node_id": 1, "project_id": P1, "project_name": "Régie A"}

    def fake_send(vmid, action, paths, origins=None, **kw):
        calls.append(("send", tuple(paths)))
        for p in paths:                           # comme le vrai send_to : rattachement après envoi
            if (origins or {}).get(p):
                mr.link_container_project(origins[p], vmid)
        return len(paths)
    mr.send_to = fake_send
    mr.set_thumb = lambda aid, t=None, reset=False: calls.append(("thumb", aid)) or {}
    mr.delete_asset = lambda aid, with_files=True: calls.append(("delete", aid))
    mr.rename_copy = lambda aid, n, p, nn: calls.append(("rename", aid)) or p
    mr.enqueue_proxy = lambda aid, node_id=None, user="", auto=False: calls.append(("proxy", aid)) or {"id": 1, "state": "queued"}
    from app import media_fetch

    def fake_fetch(aid, vmid, version=None):
        calls.append(("fetch", aid))
        mr.link_container_project(aid, vmid)
        return {"state": "done", "path": "x", "job": "j"}
    media_fetch.start = fake_fetch
    auth.check_vmid_access = lambda vmid, min_role="viewer": None
    database.db_get_container = lambda vmid: {"vmid": vmid, "project_id": P1}

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

    def ids_of(r):
        return {a["id"] for a in (r.get_json() or {}).get("assets") or []}

    def visible(who):
        as_(who)
        return ids_of(cli.get("/api/media/assets"))

    def share(targets, recipients, level="use", expires=None):
        b = {"targets": targets, "recipients": recipients, "level": level}
        if expires is not None:
            b["expires_at"] = expires
        return cli.post("/api/media/shares", json=b)

    def code(r):
        return r.status_code, (r.get_json(silent=True) or {}).get("reason")

    print("1. sans partage")
    verifier(visible("restr") == {A}, "restr : ses projets seulement")
    verifier(visible("gm") == set(), "gm (aucun projet) : rien")

    print("2. média partagé à un utilisateur (« Consulter et utiliser »)")
    as_("own")
    r = share([{"kind": "asset", "id": B}], [{"kind": "user", "id": uid["restr"]}])
    verifier(r.status_code == 200 and len(r.get_json()["shares"]) == 1, "own partage B avec restr")
    sid_b = r.get_json()["shares"][0]["id"]
    verifier(visible("restr") == {A, B}, "restr voit A et B")
    lst = {a["id"]: a for a in cli.get("/api/media/assets").get_json()["assets"]}
    verifier(lst[B].get("shared") == {"by": "own", "level": "use"} and "shared" not in lst[A], "B marqué « partagé par own »")
    fiche = cli.get("/api/media/assets/%s" % B).get_json()
    verifier(fiche.get("access") == "use" and fiche.get("shared", {}).get("by") == "own", "fiche : accès « use », partageur")
    for u in ("/api/media/assets/%s/thumb", "/api/media/assets/%s/storyboard.json", "/api/media/assets/%s/storyboard.jpg"):
        verifier(cli.get(u % B).status_code == 200, "lecture %s : admise" % u.split("%s")[1])
    verifier(ids_of(cli.post("/api/media/search", json={"query": {"q": ""}})) == {A, B}, "recherche : B compris")
    verifier(B not in ids_of(cli.get("/api/media/assets?project=%d" % P1)), "filtre projet P1 : B (de P2) n'y est pas")
    calls.clear()
    for m, u, body in (("PATCH", "/api/media/assets/%s", {"meta": {"title": "x"}}), ("POST", "/api/media/assets/%s/thumb", {"t": 1}),
                       ("DELETE", "/api/media/assets/%s", {}), ("POST", "/api/media/assets/%s/projects", {"project_id": P1}),
                       ("POST", "/api/media/assets/%s/rename_copy", {"node_id": 1, "path": "x", "new_name": "y"}),
                       ("POST", "/api/media/assets/%s/forget", {"node_id": 1, "path": "x"}),
                       ("POST", "/api/media/assets/%s/storyboard", {}), ("DELETE", "/api/media/assets/%s/proxy", {"node_id": 1})):
        verifier(code(cli.open(u % B, method=m, json=body)) == (403, "shared_read_only"), "%s %s (use) → 403" % (m, u.split("%s")[1] or " fiche"))
    verifier(code(cli.post("/api/media/proxies", json={"asset_ids": [B]})) == (403, "shared_read_only"), "proxy de B (use) → 403")
    verifier(not calls, "aucune action exécutée (%s)" % calls)
    verifier(code(cli.get("/api/media/shares?kind=asset&id=%s" % B)) == (403, "shared_read_only"), "partages de B : pas pour un destinataire")
    verifier(code(share([{"kind": "asset", "id": B}], [{"kind": "user", "id": uid["gm"]}])) == (403, "shared_read_only"),
             "restr ne repartage pas B")

    print("3. niveau « Modifier »")
    as_("own")
    r = share([{"kind": "asset", "id": B}], [{"kind": "user", "id": uid["restr"]}], level="edit")
    verifier(r.status_code == 200 and r.get_json()["shares"][0]["id"] == sid_b, "même partage mis à jour (pas de doublon)")
    as_("restr")
    verifier(cli.patch("/api/media/assets/%s" % B, json={"meta": {"title": "x"}}).status_code == 200, "edit : métadonnées admises")
    verifier(cli.post("/api/media/assets/%s/thumb" % B, json={"t": 1}).status_code == 200, "edit : vignette admise")
    verifier(code(cli.delete("/api/media/assets/%s" % B, json={})) == (403, "shared_read_only"), "edit : suppression toujours refusée")
    verifier(code(cli.post("/api/media/assets/%s/projects" % B, json={"project_id": P1})) == (403, "shared_read_only"),
             "edit : rattachement à un projet refusé")
    verifier(cli.get("/api/media/assets/%s" % B).get_json().get("access") == "edit", "fiche : accès « edit »")

    print("4. bin partagé à un groupe (vivant, borné au partageur)")
    as_("own")
    r = share([{"kind": "bin", "id": BB}], [{"kind": "group", "id": G}])
    verifier(r.status_code == 200, "own partage le bin BB avec le groupe")
    sid_bb = r.get_json()["shares"][0]["id"]
    verifier(visible("gm") == {D, E}, "gm voit D et E (sous-bin), jamais F (hors portée du partageur) : %s" % sorted(visible("gm")))
    as_("gm")
    bins = {b["id"]: b for b in cli.get("/api/media/bins").get_json()["bins"]}
    verifier(BB in bins and SUB in bins and SUBX not in bins, "bins : BB et son sous-bin, pas le sous-bin de P3")
    verifier((bins.get(BB) or {}).get("shared", {}).get("root") is True and (bins.get(SUB) or {}).get("shared", {}).get("root") is False,
             "BB marqué racine partagée, SUB sous lui")
    verifier((bins.get(BB) or {}).get("count") == 1, "compteur de BB : D seul (F exclu)")
    verifier(ids_of(cli.post("/api/media/bins/%d/assets" % BB, json={"children": True})) == {D, E}, "contenu de BB (+ sous-bins)")
    mine = cli.get("/api/media/shares/mine").get_json()
    verifier([b["bin_id"] for b in mine["bins"]] == [BB] and mine["bins"][0]["by"] == "own", "« Partagés avec moi » : BB, par own")
    verifier(code(cli.post("/api/media/bins/%d/items" % BB, json={"asset_ids": [D], "on": False})) == (403, "shared_read_only"),
             "retirer de BB en « use » → 403")
    verifier(code(cli.delete("/api/media/bins/%d" % BB)) == (403, "shared_read_only"), "supprimer BB → 403")
    verifier(code(cli.post("/api/media/bins", json={"name": "x", "parent_id": BB})) == (403, "shared_read_only"), "créer un sous-bin → 403")
    verifier(code(cli.post("/api/media/bins", json={"id": BB, "name": "x"})) == (403, "shared_read_only"), "renommer BB → 403")
    mr.bin_add(BB, [D2])                          # le propriétaire range un nouveau média
    verifier(visible("gm") == {D, E, D2}, "bin VIVANT : ce qui y entre ensuite est partagé aussi")
    as_("own")
    share([{"kind": "bin", "id": BB}], [{"kind": "group", "id": G}], level="edit")
    as_("gm")
    verifier(cli.post("/api/media/bins/%d/items" % SUB, json={"asset_ids": [D]}).status_code == 200, "« edit » : ranger dans le sous-bin")
    verifier(cli.post("/api/media/bins/%d/items" % BB, json={"asset_ids": [D2], "on": False}).status_code == 200, "« edit » : retirer de BB")
    verifier(visible("gm") == {D, E}, "D2 retiré du bin : plus partagé")
    verifier(code(cli.patch("/api/media/assets/%s" % D, json={"meta": {"t": 1}})) == (403, "shared_read_only"),
             "bin en « edit » : ses médias restent en « use »")

    print("5. bin intelligent partagé à un projet")
    as_("own")
    verifier(share([{"kind": "bin", "id": SMART}], [{"kind": "project", "id": P1}]).status_code == 200, "own partage SMART avec P1")
    v = visible("restr")
    verifier(S in v and X not in v, "restr (P1) voit S, pas X (hors portée du partageur) : %s" % sorted(v))
    as_("restr")
    verifier(ids_of(cli.post("/api/media/bins/%d/assets" % SMART, json={})) == {S}, "contenu de SMART pour restr : S")

    print("6. média SANS projet partagé par l'admin")
    as_("adm")
    verifier(share([{"kind": "asset", "id": C}], [{"kind": "user", "id": uid["out"]}]).status_code == 200, "adm partage C avec out")
    verifier(visible("out") == {F, X, C}, "out voit C (sans projet)")

    print("7. qui partage")
    as_("out")
    verifier(share([{"kind": "asset", "id": B}], [{"kind": "user", "id": uid["gm"]}]).status_code == 404, "out ne voit pas B → 404")
    verifier(code(share([{"kind": "asset", "id": C}], [{"kind": "user", "id": uid["gm"]}])) == (403, "shared_read_only"),
             "out ne repartage pas C")
    sc_restr = ms.user_scope({"id": uid["restr"], "role": "exploitant"})
    verifier(ms.check_can_share(sc_restr, {"media.manage"}, "asset", B) == "not_owner"
             and ms.check_can_share(sc_restr, {"media.manage"}, "asset", A) is None,
             "check_can_share : périmètre PROPRIÉTAIRE (ses projets), jamais ce qu'on lui a partagé")
    auth.ROLES["exploitant"].discard("media.manage")
    as_("own")
    verifier(share([{"kind": "asset", "id": D}], [{"kind": "user", "id": uid["gm"]}]).status_code == 403, "sans media.manage → 403")
    auth.ROLES["exploitant"].add("media.manage")
    verifier(code(share([{"kind": "asset", "id": D}], [{"kind": "user", "id": uid["own"]}])) == (400, None), "avec soi-même → 400")
    verifier(cli.get("/api/media/shares/recipients?q=ges").status_code == 200, "destinataires : media.manage suffit")

    print("8. date de fin, retrait, journal")
    verifier(share([{"kind": "asset", "id": D}], [{"kind": "user", "id": uid["out"]}], expires="2020-01-01").status_code == 400,
             "date de fin passée → 400")
    r = share([{"kind": "asset", "id": D}], [{"kind": "user", "id": uid["out"]}], expires="2099-01-01")
    verifier(r.status_code == 200 and r.get_json()["shares"][0]["expires_at"] == "2099-01-01T23:59:59", "fin de journée")
    verifier(D in visible("out"), "out voit D avant l'échéance")
    ms.db().execute("UPDATE media_shares SET expires_at='2020-01-01T00:00:00' WHERE target_id=? AND recipient_id=?", (D, uid["out"]))
    ms.db().commit()
    verifier(D not in visible("out"), "échue : D n'est plus visible")
    verifier(any(e["action"] == "expire" and e["target_id"] == D for e in ms.log_of()), "échéance journalisée")
    as_("own")
    sh = cli.get("/api/media/shares?kind=asset&id=%s" % B).get_json()
    verifier(sh["can_manage"] and [s["recipient_name"] for s in sh["shares"]] == ["restr"], "partages de B (propriétaire) : restr")
    verifier([e["action"] for e in sh["log"]][::-1] == ["share", "update"], "journal de B : partage puis modification")
    verifier(cli.delete("/api/media/shares/%d" % sid_b).status_code == 200, "own retire le partage de B")
    verifier(B not in visible("restr"), "retrait IMMÉDIAT")
    verifier(any(e["action"] == "revoke" and e["target_id"] == B and e["actor"] == "own" for e in ms.log_of()), "retrait journalisé")
    as_("restr")
    verifier(cli.delete("/api/media/shares/%d" % sid_bb).status_code == 403, "un destinataire ne retire pas un partage")
    ug.set_members(G, [])
    verifier(visible("gm") == set(), "quitter le groupe : plus rien")
    ug.set_members(G, [uid["gm"]])
    verifier(visible("gm") == {D, E}, "y revenir : de nouveau")
    database.db_set_project_member(P3, uid["own"], "viewer")
    verifier(F in visible("gm"), "le partageur entre dans P3 : F (rangé dans BB) apparaît — portée VIVANTE du partageur")
    database.db_remove_project_member(P3, uid["own"])

    print("9. charger un média partagé dans un conteneur (projet P1)")
    as_("own")
    share([{"kind": "asset", "id": B}], [{"kind": "user", "id": uid["restr"]}])
    as_("restr")
    calls.clear()
    r = cli.post("/api/media/send", json={"vmid": 7, "action": "load", "asset_ids": [B]})
    verifier(r.status_code == 200 and calls and calls[0][0] == "send", "envoyer B au conteneur : accepté")
    pm = lambda aid: {x["project_id"] for x in c.execute("SELECT project_id FROM project_media WHERE asset_id=?", (aid,))}  # noqa: E731
    verifier(pm(B) == {P2}, "B n'est PAS rattaché au projet du conteneur (%s)" % pm(B))
    verifier(cli.post("/api/media/fetch", json={"asset_id": B, "vmid": 7}).status_code == 200 and pm(B) == {P2},
             "rapatrier B : pas rattaché non plus")
    cli.post("/api/media/send", json={"vmid": 7, "action": "load", "asset_ids": [A]})
    verifier(pm(A) == {P1}, "A (son projet) : rattachement inchangé")
    mr.link_container_project(B, 7)
    verifier(pm(B) == {P1, P2}, "sans marque (autre geste), le rattachement reprend : la marque est consommée")
    c.execute("DELETE FROM project_media WHERE asset_id=? AND project_id=?", (B, P1))
    c.commit()

    print("10. accès global inchangé, partageur supprimé")
    as_("adm")
    verifier(ids_of(cli.get("/api/media/assets")) == {A, B, C, D, E, F, S, X, D2}, "admin : tout")
    verifier("shared" not in json.dumps(cli.get("/api/media/assets").get_json()["assets"][0]), "admin : aucun marquage")
    database.db_delete_user(uid["own"])
    verifier(visible("gm") == set() and B not in visible("restr"), "partageur supprimé : ses partages sont inertes")

    print("11. groupes d'utilisateurs")
    as_("restr")
    verifier(cli.get("/api/user-groups").status_code == 403 and cli.get("/groups").status_code in (302, 403),
             "groupes : réservés à settings.edit")
    as_("adm")
    r = cli.post("/api/user-groups", json={"name": "Rédaction", "members": [uid["restr"], 99999]})
    gid = (r.get_json() or {}).get("id")
    verifier(r.status_code == 200 and [m["username"] for m in r.get_json()["group"]["members"]] == ["restr"], "créer + membres (inconnu ignoré)")
    verifier(cli.post("/api/user-groups", json={"name": "rédaction"}).status_code == 400, "nom déjà pris (casse ignorée)")
    verifier(cli.patch("/api/user-groups/%d" % gid, json={"name": "Rédac sport"}).get_json()["group"]["name"] == "Rédac sport", "renommer")
    verifier(cli.get("/groups").status_code == 200, "page /groups")
    verifier(cli.delete("/api/user-groups/%d" % gid).status_code == 200 and ug.get_group(gid) is None, "supprimer")

    print("12. macros")
    from app import core_plugins
    acts = {a["service"]: a for a in core_plugins.service_actions()}
    verifier("media_sharing" in acts and {x["id"] for x in acts["media_sharing"]["actions"]} == {"share_bin", "unshare_bin"},
             "fournisseur du cœur inscrit (share_bin, unshare_bin)")
    opts = ms.action_options("share_bin", "bin", P1)
    verifier(str(SMART) in {o["value"] for o in opts} and str(BB) not in {o["value"] for o in opts}, "bins proposés : ceux du projet de la macro")
    verifier("group:%d" % G in {o["value"] for o in ms.action_options("share_bin", "recipient")}, "destinataires : groupes")
    r = core_plugins.run_service_action("media_sharing", "share_bin", {"bin": str(SMART), "recipient": "group:%d" % G,
                                        "level": "use", "days": 2}, ctx={"project_id": P1, "user": "restr"})
    verifier(r.get("ok") and r.get("expires_at"), "share_bin (macro de P1, lancée par restr) : partagé, avec fin")
    verifier(S not in visible("gm"), "… évalué dans la portée de restr (P1) : S (P2) n'est pas exposé")
    try:
        core_plugins.run_service_action("media_sharing", "share_bin", {"bin": str(BB), "recipient": "group:%d" % G},
                                        ctx={"project_id": P1, "user": "restr"})
        verifier(False, "bin hors du projet de la macro : refus")
    except RuntimeError:
        verifier(True, "bin hors du projet de la macro : refus")
    verifier(core_plugins.read_service_state("media_sharing", "active_shares") >= 1, "état active_shares")
    r = core_plugins.run_service_action("media_sharing", "unshare_bin", {"bin": str(SMART), "recipient": "group:%d" % G},
                                        ctx={"project_id": P1, "user": "restr"})
    verifier(r.get("removed") is True, "unshare_bin : retiré")

    print("\n%s" % ("ÉCHECS : %d" % len(ECHECS) if ECHECS else "tout est vert"))
    sys.exit(1 if ECHECS else 0)


if __name__ == "__main__":
    main()
