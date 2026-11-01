#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Câbles, vue globale inter-projets — étape 5 du chantier des E/S virtuelles (2026-10-24).

Ce que ce test vérifie, hors ligne (base jetable, topologie synthétique) :
  1. `_projects_overview` : chaque projet devient UN bloc (vmid négatif) qui n'expose que ses
     ports, avec leur état (affecté, non affecté, non publié, suit un autre projet) et leur nom ;
     les conteneurs du projet disparaissent ; une liaison DIRECTE (flux produit hors du projet,
     lu sans port) devient une entrée « sans port » ; un flux interne ou lu par un port, non ;
  2. le résumé (conteneurs, en marche, types, ports affectés, liaisons directes) ;
  3. hors projet : `project_linked` (alimente un bloc ou en est alimenté), colonne d'un
     traitement relié ;
  4. câble inter-projets : un port source SUIT un port destination — il prend ce que l'autre
     publie, suit sa republication à chaud, devient non affecté s'il disparaît ; seule une
     destination peut être suivie.

⚠ Gardes MUTÉES à l'écriture (chaque mécanisme retiré un à un) pour vérifier qu'elles mordent.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="cables_globale_db_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


from app.auth import hash_password                # noqa: E402

UID = {}
for u, role in (("admin_t", "admin"), ("ed", "exploitant")):
    database.db_create_user(u, hash_password("x"), role)
    UID[u] = database.db_get_user(u)["id"]

for vmid, hn, t in ((102, "ma", "mixer"), (103, "cca", "color_corrector"), (201, "ccb", "color_corrector")):
    database.db_upsert_container(vmid, hn)
    database.db_update_deploy_config(vmid, t, {"hostname": hn})
database.db_set_instance_uuid(201, "u-ccb")
PA = database.db_save_project("A", [102, 103])
PB = database.db_save_project("B", [201])
database.db_set_project_member(PB, UID["ed"], "editor")
CAM = database.db_create_port(PA, "source", "video", "CAM", ord_=0)
UNA = database.db_create_port(PA, "source", "video", "UNA", ord_=1)
PGM = database.db_create_port(PA, "dest", "video", "PGM", ord_=0)
NP = database.db_create_port(PA, "dest", "audio", "NP", ord_=1)
RET = database.db_create_port(PB, "source", "video", "RET")
database.db_update_port(CAM, binding={"shm": "cam1_cc"})
database.db_update_port(PGM, binding={"internal_shm": "ma_pgm"})
database.db_update_port(RET, binding={"port": PGM})

import main as app_main                           # noqa: E402
from app import auth, project_ports, tally        # noqa: E402
from app.routes import home_dashboard             # noqa: E402
app = app_main.app
app.config["TESTING"] = True
auth._snap_map_cache["ts"] = 0.0
auth._vmid_project_cache.clear()
project_ports.set_port_labels(CAM, long="Caméra plateau")


def fresh():
    tally.invalider_ports()
    tally.invalider_libelles()
    project_ports.invalidate_label_index()


def node(vmid, hn, kind, col, produces=(), consumes=(), status="running"):
    return {"vmid": vmid, "hostname": hn, "kind": kind, "col": col, "status": status,
            "node_label": "dell-1", "split": False,
            "produces": [dict(p) for p in produces], "consumes": [dict(c) for c in consumes]}


V = "video"
NODES = [
    node(301, "cam1", "decklink_io", "sources", produces=[{"shm": "cam1_cc", "kind": V}]),
    node(302, "mic", "tone_gen", "sources", produces=[{"shm": "mic", "kind": "audio"}]),
    node(102, "ma", "mixer", "composition", produces=[{"shm": "ma_pgm", "kind": V}],
         consumes=[{"shm": "cam1_cc", "kind": V, "slot": 0}]),
    node(103, "cca", "color_corrector", "composition", status="stopped",
         consumes=[{"shm": "mic", "kind": "audio", "slot": 0}, {"shm": "ma_pgm", "kind": V, "slot": 1}]),
    node(201, "ccb", "color_corrector", "composition", consumes=[{"shm": "ma_pgm", "kind": V, "slot": 0}]),
    node(303, "tx", "2110_io", "sinks", consumes=[{"shm": "ma_pgm", "kind": V, "slot": 0}]),
    node(308, "enc", "streamer", "composition", consumes=[{"shm": "ma_pgm", "kind": V, "slot": 0}]),
    node(304, "loin", "streamer", "composition", consumes=[{"shm": "zz", "kind": V, "slot": 0}]),
    node(306, "zzsrc", "player", "sources", produces=[{"shm": "zz", "kind": V}]),
]
PRODUCERS = {"cam1_cc": [{"vmid": 301, "kind": V}], "mic": [{"vmid": 302, "kind": "audio"}],
             "ma_pgm": [{"vmid": 102, "kind": V}], "zz": [{"vmid": 306, "kind": V}]}
CONSUMERS = [{"vmid": n["vmid"], "shm": c["shm"], "kind": c["kind"], "slot": c["slot"]}
             for n in NODES for c in n["consumes"]]

with app.test_request_context("/"):
    nodes, prods, cons = home_dashboard._projects_overview(NODES, PRODUCERS, CONSUMERS)
by = {n["vmid"]: n for n in nodes}
A, B = by.get(-PA) or {}, by.get(-PB) or {}

print("1. Un projet = un bloc qui n'expose que ses ports")
ok(not ({102, 103, 201} & set(by)), "les conteneurs des projets disparaissent", sorted(by))
ok(A.get("project_module") and B.get("project_module"), "un bloc par projet")
ins = {(p.get("port_id"), p.get("direct")): p for p in A.get("consumes", [])}
outs = {p.get("port_id"): p for p in A.get("produces", [])}
ok(ins.get((CAM, None), {}).get("port_state") == "assigned"
   and ins[(CAM, None)]["shm"] == "cam1_cc" and ins[(CAM, None)]["label"] == "Caméra plateau",
   "port source affecté : flux et nom du port", ins.get((CAM, None)))
ok(ins.get((UNA, None), {}).get("port_state") == "unassigned" and ins[(UNA, None)]["shm"] == "",
   "port source non affecté", ins.get((UNA, None)))
ok(outs.get(PGM, {}).get("port_state") == "assigned" and outs.get(NP, {}).get("port_state") == "unpublished",
   "destinations : publiée / non publiée", outs)
ok(outs.get(NP, {}).get("kind") == "audio", "essence du port reprise")
direct = [p for p in A.get("consumes", []) if p.get("direct")]
ok(len(direct) == 1 and direct[0]["shm"] == "mic" and direct[0]["consumers"][0]["vmid"] == 103,
   "liaison directe : le flux extérieur lu sans port", direct)
ok(not any(p.get("shm") == "ma_pgm" for p in direct), "un flux interne au projet n'est pas une liaison directe")
ok(not any(p.get("shm") == "cam1_cc" for p in direct), "un flux lu par un port n'est pas une liaison directe")
ret = (B.get("consumes") or [{}])[0]
ok(ret.get("shm") == "ma_pgm" and (ret.get("follows") or {}).get("port_id") == PGM
   and ret["follows"].get("project") == "A", "câble inter-projets : B suit PGM de A", ret)
ok(any(p["vmid"] == -PA for p in prods.get("ma_pgm", [])), "le bloc A produit ce que publie PGM")
ok({(c["vmid"], c["shm"]) for c in cons} >= {(-PA, "cam1_cc"), (-PA, "mic"), (-PB, "ma_pgm")},
   "le bloc consomme ses ports et ses liaisons directes (arêtes)")
ok(any(c["vmid"] == -PA and c["shm"] == "mic" and c.get("direct") for c in cons)
   and any(c["vmid"] == -PA and c["shm"] == "cam1_cc" and c.get("port_id") == CAM for c in cons),
   "l'arête dit « directe » ou porte son port")
ok(not any(c["vmid"] == -PA and not c["shm"] for c in cons), "un port non affecté n'a pas d'arête")

print("2. Résumé")
sm = A.get("project_summary") or {}
ok(sm.get("containers") == 2 and sm.get("running") == 1, "conteneurs et en marche", sm)
ok({t["type"]: t["n"] for t in sm.get("types", [])} == {"mixer": 1, "color_corrector": 1}, "types", sm.get("types"))
ok(sm.get("direct") == 1 and sm.get("ports") == 4 and sm.get("ports_assigned") == 2,
   "ports, affectés, liaisons directes", sm)
ok(sm.get("nodes") == ["dell-1"], "nœud", sm.get("nodes"))

print("3. Hors projet")
ok(by[301]["project_linked"] and by[302]["project_linked"] and by[303]["project_linked"],
   "relié : alimente un bloc, ou en est alimenté")
ok(by[304]["project_linked"] is False and by[306]["project_linked"] is False, "sans câble vers un projet : non relié")
ok(by[308]["col"] == "sinks" and by[304]["col"] == "composition",
   "un traitement relié passe dans la colonne de son rôle", (by[308]["col"], by[304]["col"]))

print("4. Un port source suit un port destination")
ok(project_ports.port_shm(database.db_get_port(RET)) == "ma_pgm", "port_shm suit")
fresh()
ok(tally.resolve_ref(project_ports.port_ref(RET)) == "ma_pgm", "le tally suit aussi")
LOOP = database.db_create_port(PB, "source", "video", "LOOP")
database.db_update_port(LOOP, binding={"port": LOOP})
try:
    v = project_ports.port_shm(database.db_get_port(LOOP))
except RecursionError:
    v = "RecursionError"
ok(v == "", "un port qui se suit lui-même (écrit en base) : non affecté, pas de boucle", v)
database.db_update_port(LOOP, binding={"port": CAM})
ok(project_ports.port_shm(database.db_get_port(LOOP)) == "", "suivre une SOURCE (écrit en base) : non affecté")
database.db_delete_port(LOOP)
APPLY = []
project_ports._apply = lambda vmid, kind, slot, new, old, force=False: (APPLY.append((vmid, new)) or (True, 200, {}))
database.db_port_link_set(RET, "u-ccb", "video", 0, "ma_pgm")


def client(username):
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = UID[username]
        s["username"] = username
    return cli


ADM, ED = client("admin_t"), client("ed")
r = ADM.patch("/api/projects/%d/ports/%d" % (PA, PGM), json={"binding": {"internal_shm": "ma_clean"}})
ok(r.status_code == 200 and (201, "ma_clean") in APPLY, "A republie : l'entrée de B suit à chaud", (r.status_code, APPLY))
fresh()
ok(tally.libelle_de(project_ports.port_ref(RET), 2) == "RET", "B garde son nom")
ok(tally.resolve_ref(project_ports.port_ref(RET)) == "ma_clean", "résolution à jour")
for body, lib in (({"port": CAM}, "suivre une SOURCE : refusé"), ({"port": 99999}, "suivre un port inexistant : refusé")):
    r = ADM.patch("/api/projects/%d/ports/%d" % (PB, RET), json={"binding": body})
    ok(r.status_code == 400 and database.db_get_port(RET)["binding"] == {"port": PGM}, lib, r.status_code)
r = ADM.patch("/api/projects/%d/ports/%d" % (PA, PGM), json={"binding": {"port": RET}})
ok(r.status_code == 400, "une destination ne suit rien", r.status_code)
r = ED.patch("/api/projects/%d/ports/%d" % (PB, RET), json={"binding": {"port": PGM}})
ok(r.status_code == 403, "affecter une source reste réservé à l'admin", r.status_code)
APPLY.clear()
r = ADM.delete("/api/projects/%d/ports/%d" % (PA, PGM))
ok(r.status_code == 200 and database.db_get_port(RET)["binding"] == {} and (201, "") in APPLY,
   "A supprime PGM : B devient non affecté, son entrée est vidée", (r.status_code, database.db_get_port(RET), APPLY))

print("5. Vue détaillée : nœud « Ports du projet »")
PUB = database.db_create_port(PA, "dest", "video", "PUB")
database.db_update_port(PUB, binding={"internal_shm": "ma_pgm"})
database.db_set_instance_uuid(102, "u-ma")
database.db_set_instance_uuid(103, "u-cca")
database.db_port_link_set(CAM, "u-ma", "video", 0, "cam1_cc")
database.db_port_link_set(CAM, "u-cca", "video", 1, "cam1_cc")       # lien PÉRIMÉ : 103/1 lit ma_pgm
database.db_update_port(RET, binding={"port": PUB})                   # B suit PUB
CONS2 = [{"vmid": 102, "shm": "cam1_cc", "kind": V, "slot": 0},
         {"vmid": 103, "shm": "ma_pgm", "kind": V, "slot": 1},
         {"vmid": 103, "shm": "mic", "kind": "audio", "slot": 0},
         {"vmid": 201, "shm": "ma_pgm", "kind": V, "slot": 0},            # B, qui suit PUB
         {"vmid": 303, "shm": "ma_pgm", "kind": V, "slot": 0}]            # hors projet
NODES2 = [dict(n) for n in NODES]
nodes2, cons2 = home_dashboard._project_port_nodes(NODES2, [dict(c) for c in CONS2],
                                                   database.db_get_containers(), PRODUCERS)
cards = [n for n in nodes2 if n["vmid"] == -PA]
left = next((n for n in cards if n.get("col") == "sources"), {})
right = next((n for n in cards if n.get("col") == "sinks"), {})
ok(len(cards) == 2 and all(n.get("project_ports_node") and n.get("split") for n in cards)
   and not left.get("consumes") and not right.get("produces"),
   "deux cartes de ports pour A : sources à gauche, destinations à droite", [(n.get("col"), n.get("split")) for n in cards])
outs2 = {p["port_id"]: p for p in left.get("produces", [])}
ins2 = {p["port_id"]: p for p in right.get("consumes", [])}
ok(outs2.get(CAM, {}).get("origin") == {"host": "cam1"} and outs2.get(UNA, {}).get("origin") is None,
   "port source : d'où vient le signal (producteur), rien s'il n'est pas affecté", outs2.get(CAM))
ok(ins2.get(PUB, {}).get("targets") == [{"host": "tx"}, {"port": "RET", "project": "B"}],
   "port destination : lecteurs hors projet + projets qui le suivent, sans leurs conteneurs ni l'intérieur",
   ins2.get(PUB, {}).get("targets"))
outsB = {p["port_id"]: p for n in nodes2 if n["vmid"] == -PB for p in n.get("produces", [])}
ok(outsB.get(RET, {}).get("origin") == {"port": "PUB", "project": "A"}, "port qui suit : origine = le port suivi",
   outsB.get(RET))
ok(outs2.get(CAM, {}).get("shm") == "port:%d" % CAM and outs2[CAM]["port_kind"] == "source"
   and outs2[CAM]["bound"] == "cam1_cc", "sortie = port source, adressé `port:<id>`", outs2.get(CAM))
ok(outs2.get(UNA, {}).get("port_state") == "unassigned", "port source non affecté signalé")
ok(ins2.get(PUB, {}).get("shm") == "ma_pgm" and ins2[PUB]["port_kind"] == "dest"
   and ins2.get(NP, {}).get("port_state") == "unpublished", "entrées = ports destinations", ins2)
ok(any(c["vmid"] == -PA and c["shm"] == "ma_pgm" and c.get("port_id") == PUB for c in cons2),
   "la sortie publiée arrive sur le port destination (arête)")
via = {(c["vmid"], c["slot"]): c.get("via_port") for c in cons2 if c["vmid"] > 0}
ok((via.get((102, 0)) or {}).get("ref") == "port:%d" % CAM and via[(102, 0)]["vmid"] == -PA,
   "entrée liée : son câble part du port", via.get((102, 0)))
ok(via.get((103, 1)) is None, "lien périmé (l'entrée lit autre chose) : câble physique", via.get((103, 1)))
ok(via.get((103, 0)) is None, "entrée non liée : câble physique")
# L'ENTRÉE liée nomme la virtuelle, pas le flux physique (Cyril, 28/10 : le multiview affichait
# « Atelier-decklink-io » au lieu de « Toto 1 »).
_in = {(n["vmid"], c.get("slot")): c for n in nodes2 if n["vmid"] > 0 for c in n.get("consumes", [])}
_vp = _in.get((102, 0), {}).get("via_port") or {}
_cols = project_ports.label_cols()
ok(_vp.get("name") == "CAM" and _vp.get("port_id") == CAM
   and (_vp.get("labels") or {}).get(str(_cols["long"])) == "Caméra plateau",   # posé plus haut
   "entrée liée : porte le nom et les libellés de sa virtuelle", _vp)
ok(not _in.get((103, 1), {}).get("via_port") and not _in.get((103, 0), {}).get("via_port"),
   "entrée au lien périmé ou non liée : aucun nom de virtuelle")
_rp = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "cables.html"), encoding="utf-8").read()
_rp = _rp[_rp.index("renderPort = function (side, port, idx, nodeKind, vmid) {"):][:1400]
ok("_projFilter && port.via_port" in _rp and "shm_label: txt" in _rp,
   "… et la page l'affiche dans un projet, à la place du flux physique")

# Virtuelles (28/10) : le serveur dit à quelle vidéo un port est rattaché, dans les DEUX vues ; la
# page groupe les rattachés sous la ligne de leur vidéo (lignes superposées, même point d'accroche)
# et un flux d'une autre essence lâché sur la ligne va à un rattaché de cette essence.
_kid = project_ports.add_child(database.db_get_port(CAM), "audio")
fresh()
with app.test_request_context("/"):
    _n3, _, _ = home_dashboard._projects_overview(NODES, PRODUCERS, CONSUMERS)
_cons = {c.get("port_id"): c for n in _n3 if n["vmid"] == -PA for c in n.get("consumes", [])}
_n4, _ = home_dashboard._project_port_nodes([dict(n) for n in NODES], [dict(c) for c in CONS2],
                                            database.db_get_containers(), PRODUCERS)
_outs = {p["port_id"]: p for n in _n4 if n["vmid"] == -PA for p in n.get("produces", [])}
ok(_kid and _cons.get(_kid, {}).get("parent_port_id") == CAM and _cons[_kid].get("suffix") == "A1"
   and _cons.get(CAM, {}).get("parent_port_id") is None,
   "vue globale : un rattaché porte sa vidéo et son suffixe", _cons.get(_kid))
ok(_outs.get(_kid, {}).get("parent_port_id") == CAM, "vue détaillée : idem", _outs.get(_kid))
import re as _re  # noqa: E402
_tpl0 = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "templates", "cables.html"), encoding="utf-8").read()
ok(_tpl0.count("_projPortRows('in'") + _tpl0.count("_projPortRows('out'") == 4,
   "les deux vues passent par _projPortRows (entrées et sorties)")
# Une ligne PAR FLUX, comme un conteneur (Cyril, 28/10 : « des câbles pour chaque essence ») : la
# vidéo, puis ses rattachés visibles, chacun avec son point. Rendu exécuté sous Node.
import json as _json, subprocess as _sp  # noqa: E402
_fns = "".join((_re.search(r"\nfunction %s\(.*?\n\}" % f, _tpl0, _re.S) or [""])[0]
               for f in ("_projPortRows", "_projPortRowsFlat", "_projKidName", "_projPortRow"))
_js = _fns + """
const escapeHtml = s => String(s == null ? '' : s), T = k => k, _projPortVia = () => 'de cam';
const window = {t: k => ({'projects.port_media_audio': 'Audio', 'projects.port_media_anc': 'ANC'})[k] || k};
const P = [{port_id: 1, label: 'Toto 1', kind: 'video', shm: 'v'},
           {port_id: 2, label: 'Toto 1 A1', kind: 'audio', shm: 'a', parent_port_id: 1, suffix: 'A1'},
           {port_id: 3, label: 'Toto 1 Anc', kind: 'data', shm: 'd', parent_port_id: 1, suffix: 'Anc'}];
const html = _projPortRows('out', P, (p, i) => i);
const lis = html.split('<li').slice(1);
console.log(JSON.stringify({n: lis.length, shm: lis.map(l => (l.match(/data-shm="([^"]*)"/) || [])[1]),
    kid: lis.map(l => l.includes('topo-port-kid') && l.includes('data-parent-port-id="1"')),
    names: lis.map(l => (l.match(/<span class="topo-port-label">(?:<span[^>]*>[^<]*<\\/span>)?([^<]*)/) || [])[1]),
    hidden: /visibility|topo-port-kids/.test(html), dots: (html.match(/topo-port-dot /g) || []).length}));
"""
try:
    _o = _json.loads(_sp.run(["node", "-e", _js], capture_output=True, text=True, timeout=30).stdout.strip() or "{}")
except (OSError, ValueError) as _e:
    _o = {"_error": str(_e)}
ok(_o.get("n") == 3 and _o.get("shm") == ["v", "a", "d"] and _o.get("dots") == 3 and not _o.get("hidden")
   and _o.get("kid") == [False, True, True] and _o.get("names") == ["Toto 1", "Audio 1", "ANC"],
   "virtuelle : une ligne visible par flux (vidéo, ↳ Audio 1, ↳ ANC), chacune avec son point", _o)
_cpw = _re.search(r"async function commitPortWire\(.*?\n\}", _tpl0, _re.S)
_cpw = _cpw.group(0) if _cpw else ""
ok("topo-port-group" in _cpw and "data-parent-port-id=" in _cpw
   and _cpw.index("topo-port-group") < _cpw.index("_patchPort("),
   "un audio lâché sur la ligne VIDÉO va à un rattaché de l'essence AVANT l'affectation")

# Menu d'une liaison directe (28/10, recette custom-bbc146bee1) : il propose la suppression, et elle
# débranche CHAQUE entrée interne qui lit le flux (le câble part du module projet, vmid négatif).
import re  # noqa: E402
_tpl = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "cables.html"), encoding="utf-8").read()
_m = re.search(r"function showProjectEdgeMenu\(.*?\n\}", _tpl, re.S)
_direct = (_m.group(0).split("if (row.direct) {", 1)[-1].split("} else {", 1)[0]) if _m else ""
ok('onclick="deleteDirectWire(${to},' in _direct, "liaison directe : le menu propose « Supprimer ce câble »")
_d = re.search(r"async function deleteDirectWire\(.*?\n\}", _tpl, re.S)
_d = _d.group(0) if _d else ""
ok(re.search(r"for \(const (\w+) of row\.consumers.*?/api/home/unwire.*?to_vmid: \1\.vmid", _d, re.S),
   "… et débranche chaque conteneur consommateur, pas le module projet")

# ── Le libellé d'un port n'est pas coupé AVANT sa partie discriminante (29/10) ──────────────
# ⚠ UN PLAFOND EN PIXELS (160px) COUPAIT LES LIBELLÉS BIEN AVANT LE BORD DE LA CARTE. Sur une
#   carte DeckLink, les quatre ports commencent par le même « DeckLink Quad 2 SDI 6 » et ne
#   diffèrent qu'après : « — audio 1-8 ». La coupe tombait donc exactement sur ce qui les
#   distingue, et deux entrées audio déclarées, servies et AFFICHÉES se lisaient comme des
#   doublons des entrées vidéo. La largeur doit venir de la CARTE, pas d'un nombre écrit ici.
_css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "static", "css", "base.css"), encoding="utf-8").read()
_lab = re.search(r"\.topo-port-label\s*\{(.*?)\}", _css, re.S)
_lab = _lab.group(1) if _lab else ""
_lab_sans_c = re.sub(r"/\*.*?\*/", "", _lab, flags=re.S)
ok(bool(_lab), "la règle .topo-port-label existe")
ok("max-width" not in _lab_sans_c,
   "le libellé d'un port n'a PAS de plafond de largeur en dur (il suit la carte)")
ok("text-overflow: ellipsis" in _lab_sans_c and "min-width: 0" in _lab_sans_c,
   "… il tronque toujours proprement quand la carte est vraiment trop étroite")


print()
if ECHECS:
    print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
    for e in ECHECS:
        print("  - " + e)
    sys.exit(1)
print("OK — vue globale des câbles")
