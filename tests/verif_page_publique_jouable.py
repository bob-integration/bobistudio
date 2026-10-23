#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Page publique JOUABLE d'un onglet (2026-10-18) : portée des liens, relais POST, serrure du conteneur.

Hors ligne, base jetable (DB_PATH surchargé AVANT tout accès). Le conteneur est le VRAI script du
cartouchier rendu et servi par son VRAI serveur HTTP (classe `Control`) sur un port local : l'en-tête
de portée, le refus hors onglet, l'en-tête envoyé par un visiteur — tout passe par le même code
qu'en production. Seul `get_container_ip` est remplacé (127.0.0.1) et le port 8082 réécrit.

Vérifie :
  1. création : portée {tab, play} rangée avec le lien ; `play` sans onglet refusé ; `play` refusé à qui
     n'a pas `plugins.operate` ; `play` refusé pour un plugin sans `control.public_actions` ;
  2. relais POST : refusé pour un lien sans « jouer » (lecture seule, liens d'avant compris), pour un
     chemin hors `control.public_actions`, pour un autre type de plugin sans `public_actions` même si
     la base dit « jouer », hors liste d'adresses, au-delà de la taille plafond ; accepté sinon ;
  3. l'en-tête `X-Bobi-Public-Scope` reçu par le conteneur est CELUI DU LIEN, jamais celui du visiteur ;
  4. le conteneur refuse une touche, un GO ou un lecteur d'un AUTRE onglet ;
  5. chaque action acceptée laisse une ligne au journal d'exploitation, au nom du lien ;
  6. les liens en lecture seule n'ont pas changé (relais GET, refus d'un chemin non lisible) et la page
     transmet la portée au montage de la console.

Chaque garde a été MUTÉE (rapport de session du 2026-10-18).

    CART_PLUGIN_DIR=/chemin/cartouchier ./venv/bin/python tests/verif_page_publique_jouable.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
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


TMP = tempfile.mkdtemp(prefix="verif_pub_jouable_")
from app import config, database  # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "t.db")
database._tls.__dict__.pop("conn", None)
database.init_db()

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
    plug._scan()
except Exception:
    pass
MAN = plug.get("cartouchier")
HELLO = plug.get("hello_world")
if not MAN:
    print("ÉCHEC : le registre ne voit pas le cartouchier", file=sys.stderr)
    sys.exit(1)

from app.auth import hash_password  # noqa: E402
database.db_create_user("chef", hash_password("Banc-Motdepasse-2026!"), "admin")
database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")

# ── le conteneur : le script rendu, servi par son propre serveur HTTP ─────────────────────────
import numpy as np  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
_pkg = tempfile.mkdtemp(prefix="verif_pub_pkg_")
os.symlink(os.path.join(DOSSIER, "src"), os.path.join(_pkg, "cart"))
sys.path.insert(0, _pkg)
from cart import engine as E  # noqa: E402

CART = {"version": 3, "tab": "t1",
        "tabs": [{"id": "t1", "name": "Plateau", "layout": "2h", "zones": ["za", "zb"]},
                 {"id": "t2", "name": "Antenne", "layout": "2v", "zones": ["zj", "zp"]}],
        "zones": [
            {"id": "za", "mode": "grid", "players": [1], "pages": [{"id": "pa", "keys": [
                {"id": "a1", "slot": 0, "title": "A1", "media": {"asset": "x"}}]}]},
            {"id": "zb", "mode": "grid", "players": [2], "pages": [{"id": "pb", "keys": [
                {"id": "b1", "slot": 0, "title": "B1", "media": {"asset": "x"}}]}]},
            {"id": "zj", "mode": "grid", "players": [5], "pages": [{"id": "pj", "keys": [
                {"id": "j1", "slot": 0, "title": "J1", "media": {"asset": "x"}}]}]},
            {"id": "zp", "mode": "playlist", "players": [3, 4],
             "playlist": {"items": [{"id": "i1", "media": {"asset": "x"}}], "ab": True}}]}
cfg = dict(MAN.get("deploy_defaults") or {}, players=6, cart=CART, cache_dir=os.path.join(_pkg, "cache"),
           media_root=tempfile.mkdtemp(prefix="verif_pub_media_"))
src = open(os.path.join(DOSSIER, "script.py"), encoding="utf-8").read().format(
    config=repr(cfg), hostname="cart-pub", plugin_version=MAN["version"])
LIVE = types.ModuleType("cart_pub")
LIVE.__file__ = os.path.join(_pkg, "main.py")
exec(compile(src, "<cartouchier>", "exec"), LIVE.__dict__)
LIVE.engine.source_for = lambda m: E.ArraySource(np.full((48000 * 5, 2), .5, np.float32))
RECU = []                                   # en-têtes de portée REÇUS par le conteneur


class Espion(LIVE.Control):
    def do_POST(self):
        RECU.append(self.headers.get("X-Bobi-Public-Scope"))
        return LIVE.Control.do_POST(self)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Espion)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import requests  # noqa: E402
_vrai_get, _vrai_post = requests.get, requests.post


def _reecrit(url):
    return url.replace("127.0.0.1:8082", "127.0.0.1:%d" % PORT)


requests.get = lambda url, *a, **k: _vrai_get(_reecrit(url), *a, **k)
requests.post = lambda url, *a, **k: _vrai_post(_reecrit(url), *a, **k)
from app import addressing  # noqa: E402
addressing.get_container_ip = lambda vmid: "127.0.0.1"

database.db_upsert_container(801, "cart-pub")
database.db_update_deploy_config(801, "cartouchier", {"hostname": "cart-pub", "players": 6, "cart": CART,
                                                      "plugin_version": MAN["version"]})
if HELLO:
    database.db_upsert_container(802, "hello-1")
    database.db_update_deploy_config(802, "hello_world", {"hostname": "hello-1"})

import main as app_main  # noqa: E402
app = app_main.app
app.config["TESTING"] = True


def client(nom=None):
    cli = app.test_client()
    if nom:
        with database.get_db() as db:
            u = db.execute("SELECT id, username FROM users WHERE username=?", (nom,)).fetchone()
        with cli.session_transaction() as s:
            s["user_id"] = u["id"]
            s["username"] = u["username"]
    return cli


CHEF, LECTEUR, ANON = client("chef"), client("lecteur"), client()
# le lecteur est OPÉRATEUR du projet : la garde d'accès passe, c'est le DROIT plugins.operate qui manque
PID = database.db_save_project("Plateau", [801])
with database.get_db() as _db:
    _db.execute("UPDATE containers SET project_id=? WHERE vmid=801", (PID,))
    _db.commit()
    _uid = _db.execute("SELECT id FROM users WHERE username='lecteur'").fetchone()["id"]
database.db_set_project_member(PID, _uid, "operator")
from app import auth as _auth  # noqa: E402
_auth._vmid_project_cache.clear()
_auth._snap_map_cache["ts"] = 0.0


def creer(cli, vmid, scope, titre="Lien", cidrs=""):
    r = cli.post("/api/containers/%d/share_page" % vmid, json={"title": titre, "scope": scope, "cidrs": cidrs})
    return r.status_code, r.get_json() or {}


def journal_public():
    with database.get_db() as db:
        try:
            return [dict(x) for x in db.execute(
                "SELECT actor_kind, actor_name, endpoint FROM ops_journal WHERE actor_kind='public'").fetchall()]
        except Exception:
            return []


try:
    print("\n1. Création des liens")
    c_ro, ro = creer(CHEF, 801, {"tab": "t1"}, "Plateau lecture")
    c_pl, pl = creer(CHEF, 801, {"tab": "t1", "play": True}, "Plateau jouable")
    c_old, old = creer(CHEF, 801, None, "Ancien")
    ok(c_ro == 200 and ro.get("scope") == {"tab": "t1"} and c_pl == 200
       and pl.get("scope") == {"tab": "t1", "play": True} and c_old == 200 and old.get("scope") == {},
       "portée rangée avec le lien (onglet seul, onglet + jouer, aucune)", [ro.get("scope"), pl.get("scope")])
    c, j = creer(CHEF, 801, {"play": True})
    ok(c == 400, "« jouer » sans onglet : refusé", j)
    c, j = creer(LECTEUR, 801, {"tab": "t1", "play": True})
    c2, j2 = creer(LECTEUR, 801, {"tab": "t1"})
    ok(c == 403 and c2 == 200, "« jouer » refusé à qui n'a pas plugins.operate (lecture seule permise)", [c, j, c2])
    if HELLO:
        c, j = creer(CHEF, 802, {"tab": "t1", "play": True})
        ok(c == 400, "« jouer » refusé pour un plugin sans control.public_actions", j)

    T_RO, T_PL, T_OLD = ro["token"], pl["token"], old["token"]

    print("\n2. Relais POST")
    r = ANON.post("/p/%s/plugin/fire" % T_PL, json={"key": "a1"})
    ok(r.status_code == 200 and LIVE.engine.holder("a1") is not None and LIVE.engine.holder("a1").n == 1,
       "lien jouable : une touche de SON onglet est lancée, sans compte", (r.status_code, r.get_data(as_text=True)[:200]))
    n_recu = len(RECU)
    r = ANON.post("/p/%s/plugin/fire" % T_RO, json={"key": "a1"})
    r2 = ANON.post("/p/%s/plugin/fire" % T_OLD, json={"key": "a1"})
    ok(r.status_code == 403 and r2.status_code == 403 and len(RECU) == n_recu,
       "lien sans « jouer » (portée lecture, ou lien d'avant) : refusé AU RELAIS, rien n'atteint le conteneur",
       [r.status_code, r2.status_code])
    r = ANON.post("/p/%s/plugin/key" % T_PL, json={"page": "pa", "slot": 3, "key": {"title": "x"}})
    r2 = ANON.post("/p/%s/plugin/layout" % T_PL, json={})
    ok(r.status_code == 403 and r2.status_code == 403 and len(RECU) == n_recu,
       "chemin hors control.public_actions (édition, disposition) : refusé au relais", [r.status_code, r2.status_code])
    if HELLO:
        tok = "tokhello" + "x" * 16
        database.db_create_share_link(tok, 802, "hello_world", "h", "", kind="page",
                                      instance_uuid=(database.db_get_container(802) or {}).get("instance_uuid") or "",
                                      scope={"tab": "t1", "play": True})
        r = ANON.post("/p/%s/plugin/couleurs" % tok, json={})
        ok(r.status_code == 403, "autre plugin SANS public_actions : refusé même si la base dit « jouer »", r.status_code)
    tok2 = "toknotab" + "y" * 16
    database.db_create_share_link(tok2, 801, "cartouchier", "sans onglet", "", kind="page",
                                  instance_uuid=(database.db_get_container(801) or {}).get("instance_uuid") or "",
                                  scope={"play": True})
    n_recu = len(RECU)
    r = ANON.post("/p/%s/plugin/fire" % tok2, json={"key": "a1"})
    ok(r.status_code == 403 and len(RECU) == n_recu,
       "« jouer » rangé SANS onglet (base écrite à la main) : lu comme lecture seule, refusé au relais", r.status_code)
    r = ANON.post("/p/%s/plugin/fire" % T_PL, data="x" * 20000, content_type="application/json")
    ok(r.status_code == 413, "corps au-delà du plafond : refusé", r.status_code)
    c, cid = creer(CHEF, 801, {"tab": "t1", "play": True}, "Restreint", cidrs="10.99.0.0/16")
    r = ANON.post("/p/%s/plugin/fire" % cid["token"], json={"key": "a1"})
    ok(c == 200 and r.status_code == 403, "liste d'adresses : appliquée au relais POST aussi", r.status_code)

    print("\n3. En-tête de portée")
    RECU.clear()
    r = ANON.post("/p/%s/plugin/fire" % T_PL, json={"key": "j1"},
                  headers={"X-Bobi-Public-Scope": json.dumps({"tab": "t2", "play": True})})
    ok(RECU and json.loads(RECU[-1]) == {"tab": "t1", "play": True},
       "le conteneur reçoit la portée DU LIEN, jamais celle envoyée par le visiteur", RECU)
    ok(r.status_code == 403 and LIVE.engine.holder("j1") is None,
       "… donc une touche d'un autre onglet reste refusée malgré l'en-tête forgé", (r.status_code, r.get_data(as_text=True)[:200]))

    print("\n4. Serrure du conteneur")
    r1 = ANON.post("/p/%s/plugin/pl/go" % T_PL, json={"zone": "zp"})
    r2 = ANON.post("/p/%s/plugin/player/play" % T_PL, json={"player": 3})
    r3 = ANON.post("/p/%s/plugin/player/stop" % T_PL, json={"player": 1})
    ok(r1.status_code == 403 and r2.status_code == 403 and r3.status_code == 200
       and (r1.get_json() or {}).get("error") == "out_of_scope",
       "GO d'une zone et lecteur d'un autre onglet : refusés par le conteneur ; lecteur de l'onglet : admis",
       [r1.status_code, r2.status_code, r3.status_code])

    print("\n5. Journal d'exploitation")
    jp = journal_public()
    ok(any(x["actor_name"] == "Plateau jouable" and x["endpoint"] == "/fire" for x in jp)
       and all(x["actor_name"] in ("Plateau jouable",) for x in jp) and len(jp) == 2,
       "chaque action publique ACCEPTÉE est journalisée au nom du lien (refus non journalisés)", jp)

    print("\n6. Lecture seule inchangée")
    r = ANON.get("/p/%s/plugin/state" % T_RO)
    r2 = ANON.get("/p/%s/plugin/state" % T_OLD)
    r3 = ANON.get("/p/%s/plugin/fire" % T_OLD)
    ok(r.status_code == 200 and r2.status_code == 200 and (r2.get_json() or {}).get("type") == "cartouchier"
       and r3.status_code == 403, "relais GET : lecture servie, chemin non lisible refusé (comme avant)",
       [r.status_code, r2.status_code, r3.status_code])
    html = ANON.get("/p/%s" % T_PL).get_data(as_text=True)
    html_old = ANON.get("/p/%s" % T_OLD).get_data(as_text=True)
    ok('var SCOPE = {"play": true, "tab": "t1"}' in html and "tab: SCOPE.tab || null, play: SCOPE.play === true" in html
       and "var SCOPE = {}" in html_old, "la page transmet la portée au montage (et {} pour un lien d'avant)",
       [x for x in html.splitlines() if "SCOPE" in x][:3])
finally:
    srv.shutdown()
    requests.get, requests.post = _vrai_get, _vrai_post
    if _lien:
        os.unlink(_lien)
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(_pkg, ignore_errors=True)

print("\n%d contrôle(s), %d échec(s)." % (len(OK) + len(ECHECS), len(ECHECS)))
sys.exit(1 if ECHECS else 0)
