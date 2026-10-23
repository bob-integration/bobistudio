#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Lien public limité à un onglet : ce qu'il peut LIRE (cartouchier 0.1.10, 2026-10-18).

Avant : la page n'AFFICHAIT qu'un onglet, mais /doc et /state relayés portaient tous les onglets à qui tenait
le lien (titres, fichiers, identifiants). Maintenant le cartouchier coupe chaque lecture à l'onglet de la portée
(`X-Bobi-Public-Scope`, posé par le relais, jamais celui du visiteur).

Même banc que verif_page_publique_jouable : le VRAI script du cartouchier rendu et servi par son VRAI serveur
HTTP (classe `Control`) sur un port local, l'orchestrateur réel (relais /p/<jeton>/plugin/…) sur une base jetable.

Vérifie :
  1. FUZZ (documents tirés au sort, 2 à 5 onglets, zones grille / playlist / A-B, lecteurs répartis, un lecteur
     d'un autre onglet EN TRAIN DE JOUER) : pour chaque onglet et chaque lecture déclarée (avec et sans ?zone=,
     ?key=, ?item=, ?player= visant ailleurs), la réponse relayée ne contient AUCUN id, nom, titre ou fichier d'un
     autre onglet, aucun numéro ni flux de lecteur d'une autre zone ; et elle contient bien ceux de l'onglet ;
  2. lien SANS portée (lien d'avant) et lien sans onglet : réponses identiques à la lecture interne ;
  3. usurpation : un en-tête forgé par le visiteur ({} ou un autre onglet) ne change rien ; directement au
     conteneur, un en-tête illisible ne donne RIEN (jamais tout) ;
  4. éditeur de clip, montage, capture : refusés par un lien limité.

Chaque garde a été MUTÉE (rapport de session du 2026-10-18).

    CART_PLUGIN_DIR=/chemin/cartouchier ./venv/bin/python tests/verif_page_publique_portee_lecture.py
"""
import json
import os
import random
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
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or not detail else "  → " + str(detail)[:400]))


TMP = tempfile.mkdtemp(prefix="verif_pub_lecture_")
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
if not MAN:
    print("ÉCHEC : le registre ne voit pas le cartouchier", file=sys.stderr)
    sys.exit(1)
LECTURES = list((MAN.get("control") or {}).get("read_endpoints") or [])

import numpy as np  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
_pkg = tempfile.mkdtemp(prefix="verif_pub_lecture_pkg_")
os.symlink(os.path.join(DOSSIER, "src"), os.path.join(_pkg, "cart"))
sys.path.insert(0, _pkg)
from cart import engine as E  # noqa: E402

N_PLAYERS = 8
cfg = dict(MAN.get("deploy_defaults") or {}, players=N_PLAYERS, cart=E.default_doc(),
           cache_dir=os.path.join(_pkg, "cache"), media_root=tempfile.mkdtemp(prefix="verif_pub_lecture_media_"))
src = open(os.path.join(DOSSIER, "script.py"), encoding="utf-8").read().format(
    config=repr(cfg), hostname="cart-lec", plugin_version=MAN["version"])
LIVE = types.ModuleType("cart_lec")
LIVE.__file__ = os.path.join(_pkg, "main.py")
exec(compile(src, "<cartouchier>", "exec"), LIVE.__dict__)
LIVE.engine.source_for = lambda m: E.ArraySource(np.full((48000 * 30, 2), .5, np.float32))
RECU = []


class Espion(LIVE.Control):
    def do_GET(self):
        RECU.append(self.headers.get("X-Bobi-Public-Scope"))
        return LIVE.Control.do_GET(self)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Espion)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import requests  # noqa: E402
_vrai_get, _vrai_post = requests.get, requests.post
requests.get = lambda url, *a, **k: _vrai_get(url.replace("127.0.0.1:8082", "127.0.0.1:%d" % PORT), *a, **k)
from app import addressing  # noqa: E402
addressing.get_container_ip = lambda vmid: "127.0.0.1"

database.db_upsert_container(811, "cart-lec")
database.db_update_deploy_config(811, "cartouchier", {"hostname": "cart-lec", "players": N_PLAYERS,
                                                      "plugin_version": MAN["version"]})
IU = (database.db_get_container(811) or {}).get("instance_uuid") or ""

import main as app_main  # noqa: E402
app = app_main.app
app.config["TESTING"] = True
ANON = app.test_client()


def lien(scope):
    tok = "tok%016x" % random.getrandbits(64) + "z" * 8
    database.db_create_share_link(tok, 811, "cartouchier", "l", "", kind="page", instance_uuid=IU, scope=scope)
    return tok


def direct(path, header=None):
    h = {"X-Bobi-Public-Scope": header} if header is not None else {}
    r = _vrai_get("http://127.0.0.1:%d%s" % (PORT, path), headers=h, timeout=5)
    return r.status_code, r.text


# ── documents tirés au sort ───────────────────────────────────────────────────────────────────
def tirer(rng):
    """(doc, {tab: {"ids", "texts", "players"}}) — ids et textes UNIQUES et longs : une sous-chaîne d'un
    autre onglet dans une réponse ne peut être que la sienne."""
    tag = lambda p: "%s%s" % (p, "".join(rng.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(10)))
    n_tabs = rng.randint(2, 5)
    players = list(range(1, N_PLAYERS + 1))
    rng.shuffle(players)
    tabs, zones, quoi = [], [], {}
    for _ti in range(n_tabs):
        tid, tname = tag("tab"), tag("Onglet")
        layout = rng.choice(list(E.LAYOUTS))
        zids, ids, texts, pls = [], {tid}, {tname}, set()
        for _s in range(E.LAYOUTS[layout]):
            zid, zname = tag("zon"), tag("Zone")
            mode = rng.choice(["grid", "playlist"])
            mine = [players.pop() for _ in range(min(len(players), rng.randint(0, 2)))]
            pages, items = [], []
            for _p in range(rng.randint(1, 3)):
                pid, pname = tag("pag"), tag("Page")
                keys = []
                for slot in range(rng.randint(0, 4)):
                    kid, kt, kf = tag("key"), tag("Titre"), tag("fichier") + ".wav"
                    keys.append({"id": kid, "slot": slot, "title": kt, "media": {"file": kf}})
                    ids.add(kid)
                    texts |= {kt, kf}
                pages.append({"id": pid, "name": pname, "keys": keys})
                ids.add(pid)
                texts.add(pname)
            for _i in range(rng.randint(0, 4)):
                iid, it_, if_ = tag("itm"), tag("Element"), tag("piste") + ".wav"
                items.append({"id": iid, "title": it_, "media": {"file": if_}})
                ids.add(iid)
                texts |= {it_, if_}
            zones.append({"id": zid, "name": zname, "mode": mode, "players": mine, "pages": pages,
                          "playlist": {"items": items, "ab": len(mine) == 2, "ab_players": mine}})
            zids.append(zid)
            ids.add(zid)
            texts.add(zname)
            pls |= set(mine)
        tabs.append({"id": tid, "name": tname, "layout": layout, "zones": zids})
        quoi[tid] = {"ids": ids, "texts": texts, "players": pls}
    return {"version": 3, "tab": tabs[0]["id"], "tabs": tabs, "zones": zones}, quoi


def requetes(doc, quoi, tab):
    """Les lectures déclarées, nues et avec des paramètres qui visent l'onglet ET ailleurs."""
    autres = [t for t in quoi if t != tab]
    out = [p for p in LECTURES]
    for z in doc["zones"]:
        out += ["/keys?zone=%s" % z["id"], "/pages?zone=%s" % z["id"], "/items?zone=%s" % z["id"],
                "/capture?kind=grid&zone=%s" % z["id"]]
        for pg in z["pages"]:
            for k in pg["keys"]:
                out += ["/waveform?key=%s" % k["id"], "/wave?key=%s" % k["id"]]
        for it in z["playlist"]["items"]:
            out += ["/waveform?item=%s" % it["id"]]
    out += ["/waveform?player=%d" % n for n in range(1, N_PLAYERS + 1)]
    return out, autres


def fuite(texte, quoi, tab, autres, players_ok, flows_all):
    """Ce qui, dans `texte`, appartient à un autre onglet (ids, textes, lecteurs, flux)."""
    bad = []
    for t in autres:
        for s in quoi[t]["ids"] | quoi[t]["texts"]:
            if s in texte:
                bad.append(s)
    try:
        j = json.loads(texte)
    except ValueError:
        j = None
    if isinstance(j, dict):
        for p in j.get("players") or []:
            if isinstance(p, dict) and p.get("player") not in players_ok:
                bad.append("player %s" % p.get("player"))
        for i, f in enumerate(flows_all):
            if (i + 1) not in players_ok and f in texte:
                bad.append(f)
    return bad


FLOWS = list(LIVE.FLOWS)
try:
    print("\n1. Fuzz : lectures d'un lien limité à un onglet")
    rng = random.Random(20261018)
    n_req = n_bad = n_vu = 0
    exemples, manques = [], []
    for essai in range(12):
        doc, quoi = tirer(rng)
        LIVE.engine.replace_doc(doc)
        # un lecteur d'un AUTRE onglet que le premier joue (titre et touche dans son état)
        d = LIVE.engine.doc
        for z in d["zones"]:
            k = next((k for pg in z["pages"] for k in pg["keys"]), None)
            if z["players"] and k and LIVE.engine._zone_tab[z["id"]] != d["tabs"][0]["id"]:
                LIVE.engine.fire(k["id"])
                break
        for t in quoi:
            tok = lien({"tab": t, "play": bool(essai % 2)})
            players_ok = {n for z in d["zones"] if z["id"] in set(next(x for x in d["tabs"] if x["id"] == t)["zones"])
                          for n in z["players"]}
            paths, autres = requetes(d, quoi, t)
            for p in paths:
                r = ANON.get("/p/%s/plugin%s" % (tok, p))
                txt = r.get_data(as_text=True)
                n_req += 1
                bad = fuite(txt, quoi, t, autres, players_ok, FLOWS)
                if p.startswith("/waveform?") and r.status_code != 404 and (
                        any(p.endswith("=" + s) for x in autres for s in quoi[x]["ids"])
                        or (p.startswith("/waveform?player=") and int(p.split("=")[1]) not in players_ok)):
                    bad.append("waveform %s → %s" % (p, r.status_code))   # the media of another tab, even without a name
                if bad:
                    n_bad += 1
                    if len(exemples) < 5:
                        exemples.append((p, bad[:3]))
            # et ce qui est À LUI y est bien : son onglet, ses zones, ses touches
            dj = ANON.get("/p/%s/plugin/doc" % tok).get_json() or {}
            sj = ANON.get("/p/%s/plugin/state" % tok).get_json() or {}
            mine_z = set(next(x for x in d["tabs"] if x["id"] == t)["zones"])
            mine_k = {k["id"] for z in d["zones"] if z["id"] in mine_z for pg in z["pages"] for k in pg["keys"]}
            got_k = {k["id"] for z in dj.get("zones") or [] for pg in z["pages"] for k in pg["keys"]}
            if [x["id"] for x in dj.get("tabs") or []] == [t] and {z["id"] for z in dj.get("zones") or []} == mine_z \
                    and got_k == mine_k and {z["id"] for z in sj.get("zones") or []} == mine_z \
                    and {p["player"] for p in sj.get("players") or []} == players_ok:
                n_vu += 1
            elif len(manques) < 3:
                manques.append((t, [x["id"] for x in dj.get("tabs") or []], sorted(mine_z)))
        LIVE.engine.stop_all(cut=True)
    ok(n_bad == 0 and n_req > 500, "aucune réponse ne porte un id, un nom, un titre, un fichier, un lecteur, un flux "
       "ou une forme d'onde d'un autre onglet (%d lectures relayées)" % n_req, exemples)
    ok(manques == [] and n_vu > 20, "… et chaque lien lit bien SON onglet en entier (onglet, zones, touches, lecteurs ; %d liens)" % n_vu,
       manques)

    print("\n2. Liens sans portée : comme avant")
    doc, quoi = tirer(random.Random(7))
    LIVE.engine.replace_doc(doc)
    t_old, t_vide = lien(None), lien({})
    same = []
    for p in ("/doc", "/tabs", "/zones", "/pages", "/keys", "/players", "/flows", "/items"):
        a = ANON.get("/p/%s/plugin%s" % (t_old, p)).get_json()
        b = ANON.get("/p/%s/plugin%s" % (t_vide, p)).get_json()
        c = json.loads(direct(p)[1])
        same.append((p, a == c and b == c))
    st = ANON.get("/p/%s/plugin/state" % t_old).get_json() or {}
    ok(all(x[1] for x in same) and len(st.get("tabs") or []) == len(doc["tabs"])
       and len(st.get("players") or []) == N_PLAYERS,
       "lien d'avant et lien {} : mêmes réponses que la lecture interne (tous les onglets)", [x for x in same if not x[1]])

    print("\n3. Usurpation")
    t1, t2 = doc["tabs"][0]["id"], doc["tabs"][1]["id"]
    tok = lien({"tab": t1})
    RECU.clear()
    r1 = ANON.get("/p/%s/plugin/doc" % tok, headers={"X-Bobi-Public-Scope": "{}"}).get_json() or {}
    r2 = ANON.get("/p/%s/plugin/doc" % tok, headers={"X-Bobi-Public-Scope": json.dumps({"tab": t2})}).get_json() or {}
    ok([x["id"] for x in r1.get("tabs") or []] == [t1] and [x["id"] for x in r2.get("tabs") or []] == [t1]
       and RECU and all(json.loads(h) == {"tab": t1} for h in RECU),
       "en-tête forgé par le visiteur ({} ou un autre onglet) : le conteneur reçoit la portée DU LIEN", RECU[-2:])
    tok_old = lien(None)
    RECU.clear()
    r3 = ANON.get("/p/%s/plugin/doc" % tok_old, headers={"X-Bobi-Public-Scope": json.dumps({"tab": t2})}).get_json() or {}
    ok(len(r3.get("tabs") or []) == len(doc["tabs"]) and RECU == ["{}"],
       "lien d'avant + en-tête forgé : ne se restreint ni ne s'élargit (l'en-tête du visiteur n'est pas relayé)", RECU)
    bads = []
    for h in ("pas du json", "[]", '{"tab": 3}', '{"tab": ""}', '"t1"', json.dumps({"tab": "inconnu"})):
        for p in ("/doc", "/state", "/keys", "/players", "/flows", "/tabs", "/zones", "/pages"):
            code, txt = direct(p, h)
            if any(s in txt for t in quoi for s in quoi[t]["ids"] | quoi[t]["texts"]):
                bads.append((h, p))
    ok(not bads, "au conteneur directement, un en-tête illisible ou un onglet inconnu ne donne RIEN (jamais tout)", bads[:4])

    print("\n4. Éditeur, montage, capture")
    k = next(k for z in LIVE.engine.doc["zones"] if z["id"] in next(x for x in LIVE.engine.doc["tabs"] if x["id"] == t1)["zones"]
             for pg in z["pages"] for k in pg["keys"]) if any(
        pg["keys"] for z in LIVE.engine.doc["zones"] for pg in z["pages"]) else None
    codes = [ANON.get("/p/%s/plugin%s" % (tok, p)).status_code
             for p in ("/wave?asset=x&file=y.wav", "/montage/job?id=1", "/capture?kind=grid")]
    ok(codes == [403, 403, 403], "lien limité : /wave, /montage/job, /capture refusés", codes)
finally:
    srv.shutdown()
    requests.get, requests.post = _vrai_get, _vrai_post
    if _lien:
        os.unlink(_lien)
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(_pkg, ignore_errors=True)

print("\n%d contrôle(s), %d échec(s)." % (len(OK) + len(ECHECS), len(ECHECS)))
sys.exit(1 if ECHECS else 0)
