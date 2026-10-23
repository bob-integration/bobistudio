#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Sauvegardes du cartouchier côté ORCHESTRATEUR : bibliothèque à genres (app/plugin_library.py).

Hors ligne, base jetable (DB_PATH surchargé AVANT tout accès), aucun conteneur : le script du
cartouchier est RENDU et chargé en mémoire, et l'orchestrateur lui parle par une indirection
(`plugin_library._http`, `plugin_persist._url` / `requests`) — c'est le vrai moteur qui capture et
applique, la vraie table plugin_store qui range, les vraies routes Flask qui décident des droits.

Vérifie :
  1. enregistrer chaque genre (auteur = la session, nom unique PAR GENRE) ; liste filtrée par genre ;
  2. rappeler une grille / une playlist / une page dans la page N d'un AUTRE cartouchier, et la
     persistance du document rappelé (control.persist → deploy_config) ;
  3. export d'une sauvegarde → import sur une installation vierge : contenu identique, média
     inconnu de la base SIGNALÉ (jamais un refus) ; genre inconnu refusé ;
  4. droits : lister / exporter au login, enregistrer / supprimer / importer / rappeler sous
     `plugins.operate` ;
  5. macros : listes vivantes filtrées par genre, l'action « rappeler une page dans une page »
     applique sur le conteneur ; Ember+ (recall_preset sans genre) applique aussi.

Chaque garde a été MUTÉE une fois (cf. le rapport de session du 2026-10-17).

    CART_PLUGIN_DIR=/chemin/cartouchier ./venv/bin/python tests/verif_bibliotheque_cartouchier.py
"""
import copy
import os
import shutil
import sys
import tempfile
import types

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
DOSSIER = os.environ.get("CART_PLUGIN_DIR") or os.path.join(RACINE, "plugins", "cartouchier")
if not os.path.isfile(os.path.join(DOSSIER, "plugin.json")):
    print("SAUTÉ : plugin cartouchier absent — rien à vérifier.")
    sys.exit(0)

ECHECS, OK = [], []


def ok(cond, libelle, detail=""):
    (OK if cond else ECHECS).append((libelle, detail))
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or not detail else "  → " + str(detail)[:300]))


# ── base jetable AVANT tout accès ──────────────────────────────────────────────────────────
TMP = tempfile.mkdtemp(prefix="verif_biblio_cart_")
from app import config, database  # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "t.db")
database._tls.__dict__.pop("conn", None)
from app import media_registry  # noqa: E402
media_registry.MEDIA_DB_PATH = os.path.join(TMP, "m.db")
database.init_db()

# Le plugin testé est celui de CART_PLUGIN_DIR : le registre scanne plugins/, on y pose un lien.
from app import plugins as plug  # noqa: E402
_lien = None
_cible = os.path.join(RACINE, "plugins", "cartouchier")
if os.path.realpath(_cible) != os.path.realpath(DOSSIER):
    if os.path.lexists(_cible):
        print("ÉCHEC : plugins/cartouchier existe et n'est pas CART_PLUGIN_DIR", file=sys.stderr)
        sys.exit(1)
    os.symlink(DOSSIER, _cible)
    _lien = _cible
try:
    plug._scan() if hasattr(plug, "_scan") else None
except Exception:
    pass
MAN = plug.get("cartouchier")
if not MAN:
    print("ÉCHEC : le registre ne voit pas le cartouchier", file=sys.stderr)
    sys.exit(1)

from app.auth import hash_password  # noqa: E402
database.db_create_user("chef", hash_password("Banc-Motdepasse-2026!"), "admin")
database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")

# ── deux cartouchiers « vivants » : le script rendu, chargé en mémoire ─────────────────────
import numpy as np  # noqa: E402
_pkg = tempfile.mkdtemp(prefix="verif_biblio_pkg_")
os.symlink(os.path.join(DOSSIER, "src"), os.path.join(_pkg, "cart"))
sys.path.insert(0, _pkg)
_media = tempfile.mkdtemp(prefix="verif_biblio_media_")
SCRIPT = open(os.path.join(DOSSIER, "script.py"), encoding="utf-8").read()
SRCS = {}


def vivant(host):
    from cart import engine as E
    cfg = dict(MAN.get("deploy_defaults") or {}, hostname=host, cache_dir=os.path.join(_pkg, "cache_" + host),
               media_root=_media)
    src = SCRIPT.format(config=repr(cfg), hostname=host, plugin_version=MAN["version"])
    mod = types.ModuleType("cart_" + host)
    mod.__file__ = os.path.join(_pkg, host + ".py")
    exec(compile(src, "<cartouchier>", "exec"), mod.__dict__)
    mod.engine.source_for = lambda md: SRCS.get(md.get("asset"))
    mod.E = E
    return mod


LIVES = {701: vivant("cart-a"), 702: vivant("cart-b")}
for v, host in ((701, "cart-a"), (702, "cart-b")):
    database.db_upsert_container(v, host)
    database.db_update_deploy_config(v, "cartouchier", {"hostname": host, "plugin_version": MAN["version"],
                                                        "cart": LIVES[v].engine.doc_copy()})


class Resp:
    def __init__(self, code, obj):
        self.status_code, self._o = code, obj

    def json(self):
        return copy.deepcopy(self._o)


def appel(vmid, method, path, params=None, body=None):
    lv = LIVES[vmid]
    if method == "GET":
        qs = {k: [v] for k, v in (params or {}).items()}
        return Resp(*lv._get(path, qs))
    try:
        r = lv._post(path, copy.deepcopy(body or {}))
    except lv.E.EngineError as ex:
        return Resp(ex.status, {"ok": False, "error": ex.code, "params": ex.params})
    out = {"ok": True}
    if isinstance(r, dict):
        out.update(r if "zones" not in r else {"doc": r})
    return Resp(200, out)


from app import plugin_library as L  # noqa: E402
from app import plugin_persist as pp  # noqa: E402
L._http = lambda method, vmid, path, **kw: appel(vmid, method, path, kw.get("params"), kw.get("json"))
pp._url = lambda vmid, path: "fake://%d%s" % (vmid, path)
pp._refresh_script = lambda *a, **k: None


class FakeReq:
    @staticmethod
    def get(url, timeout=None, **kw):
        vmid, path = int(url[7:].split("/", 1)[0]), "/" + url[7:].split("/", 1)[1]
        return appel(vmid, "GET", path)


pp.requests = FakeReq

import main as app_main  # noqa: E402
app = app_main.app
app.config["TESTING"] = True


def client(nom):
    cli = app.test_client()
    with database.get_db() as db:
        u = db.execute("SELECT id, username FROM users WHERE username=?", (nom,)).fetchone()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]
    return cli


# Le lecteur est OPÉRATEUR du projet des deux cartouchiers : il passe la garde d'accès au conteneur,
# et c'est donc bien le DROIT `plugins.operate` (qu'un rôle global « viewer » n'a pas) qui le refuse.
PID = database.db_save_project("Plateau", [701, 702])
with database.get_db() as _db:
    _db.execute("UPDATE containers SET project_id=? WHERE vmid IN (701, 702)", (PID,))
    _db.commit()
    _uid = _db.execute("SELECT id FROM users WHERE username='lecteur'").fetchone()["id"]
database.db_set_project_member(PID, _uid, "operator")
from app import auth as _auth  # noqa: E402
_auth._vmid_project_cache.clear()           # caches d'appartenance (TTL) remplis avant le rattachement
_auth._snap_map_cache["ts"] = 0.0
CHEF, LECTEUR = client("chef"), client("lecteur")
A, B = LIVES[701], LIVES[702]

# Contenu de A : deux pages, une playlist. « j1 » est connu de la base des médias, « j9 » non.
SRCS.update({"j1": A.E.ArraySource(np.full((48000 * 3, 2), .5, np.float32)),
             "j2": A.E.ArraySource(np.full((48000 * 3, 2), .3, np.float32))})
A._post("/key", {"page": "p1", "slot": 0, "key": {"title": "Jingle", "media": {"asset": "j1"}}})
A._post("/key", {"page": "p1", "slot": 1, "key": {"title": "Tapis", "media": {"asset": "j9", "file": "tapis.wav"}}})
A._post("/pages", {"op": "add", "name": "Météo"})
A._post("/key", {"page": A.engine.doc["zones"][0]["page"], "slot": 2, "key": {"title": "Météo", "media": {"asset": "j2"}}})
A._post("/playlist", {"op": "add", "items": [{"title": "Ouverture", "media": {"asset": "j1"}},
                                             {"title": "Fin", "media": {"asset": "j2"}}]})
P1_A = A.engine.doc["zones"][0]["pages"][0]["id"]
now = "2026-10-17T00:00:00"
media_registry.db().execute("INSERT INTO media_assets (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                            ("j1", "jingle", now, now))
media_registry.db().commit()

T = "/api/plugins/cartouchier"
try:
    # ── 1. Enregistrer ─────────────────────────────────────────────────────────────────────
    print("\n1. Enregistrer chaque genre")
    r = CHEF.post("/api/containers/701/library", json={"kind": "grid", "name": "Matinale"})
    g = r.get_json()
    ok(r.status_code == 200 and g.get("kind") == "grid" and g["summary"]["pages"] == 2, "grille enregistrée", g)
    r = CHEF.post("/api/containers/701/library", json={"kind": "playlist", "name": "Matinale"})
    ok(r.status_code == 200, "même nom pour un AUTRE genre : accepté (identité = genre + nom)", r.get_json())
    r = CHEF.post("/api/containers/701/library", json={"kind": "page", "name": "Jingles", "page": P1_A})
    pg = r.get_json()
    ok(r.status_code == 200 and pg["summary"]["keys"] == 2, "page enregistrée (désignée par son id)", pg)
    r = CHEF.post("/api/containers/701/library", json={"kind": "grid", "name": "Matinale"})
    ok(r.status_code == 409 and r.get_json().get("error") == "name_taken", "même genre, même nom : 409 name_taken",
       r.get_json())
    r = CHEF.post("/api/containers/701/library", json={"kind": "grid", "name": "Matinale", "overwrite": True})
    ok(r.status_code == 200 and r.get_json()["id"] == g["id"] and r.get_json()["created"] is False,
       "remplacer explicitement garde la même entrée", r.get_json())
    r = CHEF.post("/api/containers/701/library", json={"kind": "tout", "name": "X"})
    ok(r.status_code == 400 and r.get_json().get("error") == "bad_kind", "genre inconnu : code bad_kind")
    r = CHEF.post("/api/containers/701/library", json={"kind": "grid", "name": "  "})
    ok(r.status_code == 400 and r.get_json().get("error") == "bad_name", "nom vide : code bad_name")
    lst = CHEF.get(T + "/library?kind=grid").get_json()["saves"]
    ok([s["name"] for s in lst] == ["Matinale"] and lst[0]["author"] == "chef" and lst[0]["kind"] == "grid"
       and lst[0]["updated_at"], "liste filtrée par genre, avec auteur (la session) et date", lst)
    ok(len(CHEF.get(T + "/library").get_json()["saves"]) == 3, "sans genre : toute la bibliothèque")

    # ── 2. Rappeler ────────────────────────────────────────────────────────────────────────
    # Zone (cartouchier 0.1.8) : l'orchestrateur TRANSMET la zone visée à la capture et au rappel — une
    # zone inconnue du conteneur revient donc en refus ; sans transmission, la zone par défaut aurait servi.
    r = CHEF.post("/api/containers/701/library", json={"kind": "playlist", "name": "Zonée", "zone": "zz"})
    zid_a = A.engine.doc["zones"][0]["id"]
    r2 = CHEF.post("/api/containers/701/library", json={"kind": "playlist", "name": "Zonée", "zone": zid_a})
    ok(r.status_code == 404 and (r.get_json() or {}).get("error") == "unknown_zone"
       and r2.status_code == 200 and r2.get_json()["summary"].get("zone") == zid_a,
       "capture : la zone est transmise au conteneur", [r.status_code, r.get_json(), r2.get_json()])
    r = CHEF.post("/api/containers/702/library/%d/recall" % r2.get_json()["id"], json={"zone": "zz"})
    ok(r.status_code == 404, "rappel : la zone est transmise au conteneur", [r.status_code, r.get_json()])

    print("\n2. Rappeler, sur un AUTRE cartouchier")
    sid_pl = next(s["id"] for s in CHEF.get(T + "/library?kind=playlist").get_json()["saves"])
    grille_b = copy.deepcopy(B.engine.doc["zones"][0]["pages"])
    r = CHEF.post("/api/containers/702/library/%d/recall" % sid_pl, json={})
    ok(r.status_code == 200 and [it["title"] for it in B.engine.doc["zones"][0]["playlist"]["items"]] == ["Ouverture", "Fin"]
       and B.engine.doc["zones"][0]["pages"] == grille_b, "playlist rappelée sur B, sa grille intacte", r.get_json())
    ok(pp.flush(702) is True and (pp._dc(702)["params"]["cart"]["zones"][0]["playlist"]["items"][0]["title"] == "Ouverture"),
       "le document rappelé est PERSISTÉ (control.persist.settings contient /recall)")
    B._post("/pages", {"op": "add", "name": "Deux"})
    B._post("/pages", {"op": "add", "name": "Trois"})
    r = CHEF.post("/api/containers/702/library/%d/recall" % pg["id"], json={"page": "2"})
    rep = r.get_json().get("recall") or {}
    p2 = B.engine.doc["zones"][0]["pages"][1]
    ok(r.status_code == 200 and {k["title"] for k in p2["keys"]} == {"Jingle", "Tapis"}
       and p2["name"] == "Deux" and len(B.engine.doc["zones"][0]["pages"]) == 3,
       "page rappelée DANS la page 2 de B (remplacée ; page source sans nom : le nom cible reste)", rep)
    ok(rep.get("missing_count") == 2, "rapport du rappel : sons absents du nœud listés", rep)
    r = CHEF.post("/api/containers/702/library/%d/recall" % g["id"], json={})
    ok(r.status_code == 200 and [p["name"] for p in B.engine.doc["zones"][0]["pages"]] == ["", "Météo"]
       and [it["title"] for it in B.engine.doc["zones"][0]["playlist"]["items"]] == ["Ouverture", "Fin"],
       "grille rappelée sur B, sa playlist intacte", [p["name"] for p in B.engine.doc["zones"][0]["pages"]])
    r = CHEF.post("/api/containers/702/library/99999/recall", json={})
    ok(r.status_code == 404 and r.get_json().get("error") == "unknown_save", "sauvegarde inconnue : 404 unknown_save")

    # ── 3. Export / import ─────────────────────────────────────────────────────────────────
    print("\n3. Export → import sur une installation vierge")
    doc = CHEF.get(T + "/presets-export?kind=page&id=%d" % pg["id"]).get_json()
    ok(doc.get("count") == 1 and doc["presets"][0]["value"]["kind"] == "page", "export d'UNE sauvegarde", doc.get("count"))
    tout = CHEF.get(T + "/presets-export?kind=grid").get_json()
    ok(tout["count"] == 1 and tout["presets"][0]["name"] == "Matinale", "export d'un genre entier")
    avant = copy.deepcopy(doc["presets"][0]["value"])
    for s in CHEF.get(T + "/library").get_json()["saves"]:
        CHEF.delete(T + "/library/%d" % s["id"])
    ok(CHEF.get(T + "/library").get_json()["saves"] == [], "bibliothèque vidée (installation « vierge »)")
    # sur l'autre installation, « j1 » n'existe pas non plus
    media_registry.db().execute("DELETE FROM media_assets")
    media_registry.db().commit()
    r = CHEF.post(T + "/presets-import", json={"document": doc, "mode": "skip"})
    imp = r.get_json()
    ok(r.status_code == 200 and imp["counts"]["imported"] == 1, "import accepté", imp)
    ok(imp["counts"]["missing_media"] == 2 and {m["asset"] for m in imp["missing_media"][0]["media"]} == {"j1", "j9"},
       "média inconnu de la base SIGNALÉ, jamais un refus", imp.get("missing_media"))
    back = L.find("cartouchier", "page", "Jingles")
    ok(back is not None and {k: v for k, v in back["value"].items() if k != "missing_media"} == avant
       and back["value"]["missing_media"] == 2, "aller-retour identique (plus le compte des médias manquants)")
    r = CHEF.post(T + "/presets-import", json={"document": doc, "mode": "skip"})
    ok(r.get_json()["counts"]["skipped"] == 1, "réimport en skip : ignoré (même genre, même nom)")
    faux = copy.deepcopy(doc)
    faux["presets"][0]["value"]["kind"] = "show"
    faux["presets"][0]["name"] = "Autre"
    r = CHEF.post(T + "/presets-import", json={"document": faux})
    ok(r.get_json()["counts"]["rejected"] == 1 and r.get_json()["rejected"][0]["reason"] == "bad_kind",
       "un genre que le plugin ne déclare pas est REFUSÉ (code)", r.get_json())

    # ── 4. Droits ──────────────────────────────────────────────────────────────────────────
    print("\n4. Droits")
    from app import auth as _auth
    _cu = _auth.current_user
    _auth.current_user = lambda: database.db_get_user_by_id(_uid)
    try:
        with app.test_request_context():
            ok(_auth.check_vmid_access(702, "operator") is None,
               "lecteur : la garde d'accès au conteneur le LAISSE passer (opérateur du projet)")
    finally:
        _auth.current_user = _cu
    ok(LECTEUR.get(T + "/library?kind=page").status_code == 200, "lecteur : lister")
    ok(LECTEUR.get(T + "/presets-export?kind=page").status_code == 200, "lecteur : exporter")
    ok(LECTEUR.post("/api/containers/701/library", json={"kind": "grid", "name": "Z"}).status_code == 403,
       "lecteur : enregistrer refusé")
    ok(LECTEUR.post("/api/containers/702/library/%d/recall" % back["id"], json={}).status_code == 403,
       "lecteur : rappeler refusé")
    ok(LECTEUR.delete(T + "/library/%d" % back["id"]).status_code == 403, "lecteur : supprimer refusé")
    ok(LECTEUR.post(T + "/presets-import", json={"document": doc}).status_code == 403, "lecteur : importer refusé")

    # ── 5. Macros et Ember+ ────────────────────────────────────────────────────────────────
    print("\n5. Macros")
    CHEF.post("/api/containers/701/library", json={"kind": "grid", "name": "Soir"})
    from app import macros
    from app.routes import macros_api, recall_presets, recall_preset
    acts = macros_api._with_recall_options(702, "cartouchier", MAN["actions"])
    opt = {a["id"]: [o["label"] for o in next(p for p in a["params"] if p.get("options_from") == "recall")["options"]]
           for a in acts if a.get("recall_kind")}
    ok(opt == {"recall_grid": ["Soir"], "recall_playlist": [], "recall_page": ["Jingles"]},
       "listes vivantes filtrées par genre", opt)
    n = len(B.engine.doc["zones"][0]["pages"])
    macros.exec_action(702, "recall_page", {"memory": str(back["id"]), "page": "", "add": "true"})
    ok(len(B.engine.doc["zones"][0]["pages"]) == n + 1
       and {k["title"] for k in B.engine.doc["zones"][0]["pages"][-1]["keys"]} == {"Jingle", "Tapis"},
       "macro « rappeler une page » en ajout : appliquée sur le conteneur")
    macros.exec_action(702, "recall_page", {"memory": str(back["id"]), "page": "1"})
    ok({k["title"] for k in B.engine.doc["zones"][0]["pages"][0]["keys"]} == {"Jingle", "Tapis"}
       and len(B.engine.doc["zones"][0]["pages"]) == n + 1, "macro « rappeler une page dans la page 1 »")
    sid_soir = L.find("cartouchier", "grid", "Soir")["id"]
    try:
        macros.exec_action(702, "recall_playlist", {"memory": str(sid_soir)})
        ok(False, "une grille n'est pas rappelable par l'action « playlist »")
    except RuntimeError:
        ok(True, "une grille n'est pas rappelable par l'action « playlist »")
    _rc, tous = recall_presets(702, "cartouchier")
    idx = next(i for i, p in enumerate(tous) if p["id"] == sid_soir)
    okk, detail = recall_preset(702, "cartouchier", idx)
    ok(okk and [p["name"] for p in B.engine.doc["zones"][0]["pages"]] == ["", "Météo"], "Ember+ (sans genre) : rappel générique",
       detail)
finally:
    if _lien:
        os.unlink(_lien)
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(_pkg, ignore_errors=True)
    shutil.rmtree(_media, ignore_errors=True)

print("\n%d contrôle(s), %d échec(s)." % (len(OK) + len(ECHECS), len(ECHECS)))
sys.exit(1 if ECHECS else 0)
