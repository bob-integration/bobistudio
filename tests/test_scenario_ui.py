# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Tests OFFLINE de l'éditeur Scénario nodal (10e passe ch.6) — UI + API.

Sans pytest (comme tests/smoke_test.py) :
    ./venv/bin/python tests/test_scenario_ui.py

Contraintes respectées :
  - DB de prod copiée dans un fichier temporaire (jamais modifiée) ;
  - session FORGÉE sur le test client Flask (cookie signé par la secret key de la
    copie de DB) — aucun serveur lancé, aucun container live touché ;
  - node --check sur chaque <script> inline du workspace RENDU (Jinja résolu).

Couverture :
  1. rendu Jinja de /workspace/<pid> (200, éditeur Scénario présent) ;
  2. syntaxe JS de tous les scripts inline de la page rendue (node --check) ;
  3. cycle API : POST macro blocks → GET ?as=graph (champ nodal) → PATCH nodes/v2
     (positions x,y) → GET (format nodes/v2, positions persistées) → GET ?as=blocks
     (round-trip) ; graphe NON structuré (2 entrées) → structured=false et pas de
     champ blocks ; graphe invalide → 400 ; même cycle sur /api/macros (système).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)   # main.py référence templates/ static/ en relatif
os.environ["BOBI_COOKIE_SECURE"] = "0"   # test client HTTP → cookie de session émis

# DB temporaire = copie de la prod, AVANT tout import applicatif.
_tmp = tempfile.mkdtemp(prefix="bobi_scn_test_")
_db_copy = os.path.join(_tmp, "db.sqlite")
shutil.copy2(os.path.join(ROOT, "db_bobistudio.db"), _db_copy)

from app import database  # noqa: E402
database.DB_PATH = _db_copy

import main  # noqa: E402  (importe l'app Flask ; les threads ne partent que sous __main__)
from app.database import get_db  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception:
        FAILS.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc()


def _ids():
    with get_db() as db:
        u = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        p = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if not p:
            db.execute("INSERT INTO projects (name) VALUES ('scn-test')")
            db.commit()
            p = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        assert u, "aucun utilisateur admin dans la DB copiée"
        return u["id"], p["id"]


UID, PID = _ids()
client = main.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = UID   # session forgée (admin → accès global)

BLOCKS = [
    {"type": "sleep", "ms": 100},
    {"type": "if",
     "cond": {"left": {"kind": "var", "name": "x"}, "op": "==",
              "right": {"kind": "const", "value": "1"}},
     "then": [{"type": "set_var", "name": "y", "value": "ok"}],
     "else": []},
]


def norm(steps):
    out = []
    for s in steps or []:
        s = dict(s)
        if s.get("type") == "if":
            s["then"] = norm(s.get("then"))
            s["else"] = norm(s.get("else"))
        elif s.get("type") == "parallel":
            s["branches"] = [norm(b) for b in s.get("branches") or []]
        out.append(s)
    return out


# ─── 1. Rendu de la page workspace (Jinja OK, éditeur présent) ────────────
_page = {}


def t_page_renders():
    r = client.get(f"/workspace/{PID}")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    _page["html"] = html
    for marker in ('id="wsp-scn"', 'scn-seg-blocks', 'wspScnOpen', 'scn-pal',
                   'projects.scn_hint', "IS_GLOBAL"):
        # les clés i18n sont résolues au rendu : on vérifie la présence des ids/JS
        if marker == 'projects.scn_hint':
            continue
        assert marker in html, f"marqueur absent : {marker}"


# ─── 2. node --check sur chaque <script> inline rendu ─────────────────────
def t_inline_js_syntax():
    node = shutil.which("node")
    assert node, "node introuvable (requis pour la vérification de syntaxe JS)"
    html = _page["html"]
    import re
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        html, flags=re.S | re.I)
    assert blocks, "aucun script inline trouvé"
    # ⚠ NE PAS chercher « le plus gros bloc ». C'est ce que faisait ce test, et le jour où un
    # AUTRE script inline a dépassé celui du workspace (576 Ko contre 172), l'assertion a sauté
    # — emportant avec elle la boucle `node --check` ci-dessous, qui est la RAISON D'ÊTRE du
    # test. Six blocs ont cessé d'être vérifiés sans que rien ne le dise : la suite affichait un
    # échec, mais sur la mauvaise cause. On cherche le bloc PAR SON CONTENU.
    wsp = [b for b in blocks if "wspScnOpen" in b]
    assert wsp, "le script du workspace n'est dans aucun <script> inline de la page rendue"
    for i, code in enumerate(blocks):
        if not code.strip():
            continue
        path = os.path.join(_tmp, f"inline_{i}.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"script inline #{i} invalide :\n{r.stderr[:2000]}"


# ─── 3. Cycle API projet : blocks → ?as=graph → PATCH nodes/v2 → ?as=blocks ─
_mid = {}


def t_api_create_blocks():
    r = client.post(f"/api/projects/{PID}/macros",
                    json={"name": "scn-ui-test", "graph": {"format": "blocks/v1",
                                                           "steps": BLOCKS}})
    assert r.status_code == 200, r.get_data(as_text=True)
    m = r.get_json()["macro"]
    _mid["p"] = m["id"]
    assert m["format"] == "blocks/v1" and m["structured"] is True


def t_api_as_graph():
    r = client.get(f"/api/projects/{PID}/macros/{_mid['p']}?as=graph")
    assert r.status_code == 200
    m = r.get_json()["macro"]
    g = m.get("nodal")
    assert g and g["format"] == "nodes/v2", "champ nodal absent"
    types = [n["type"] for n in g["nodes"]]
    assert "entry" in types and "cond" in types and "sleep" in types
    assert all("x" in n and "y" in n for n in g["nodes"]), "positions absentes"
    _mid["nodal"] = g


def t_api_patch_nodes_v2():
    g = json.loads(json.dumps(_mid["nodal"]))
    for i, n in enumerate(g["nodes"]):   # positions éditées (drag simulé)
        n["x"], n["y"] = 100 + 250 * i, 80 + 40 * i
    r = client.patch(f"/api/projects/{PID}/macros/{_mid['p']}",
                     json={"name": "scn-ui-test", "graph": g})
    assert r.status_code == 200, r.get_data(as_text=True)
    m = r.get_json()["macro"]
    assert m["format"] == "nodes/v2" and m["structured"] is True
    r = client.get(f"/api/projects/{PID}/macros/{_mid['p']}")
    m = r.get_json()["macro"]
    assert m["format"] == "nodes/v2"
    assert m["graph"]["nodes"][1]["x"] == 350, "positions x,y non persistées"


def t_api_as_blocks_roundtrip():
    r = client.get(f"/api/projects/{PID}/macros/{_mid['p']}?as=blocks")
    assert r.status_code == 200
    m = r.get_json()["macro"]
    assert norm(m.get("blocks")) == norm(BLOCKS), \
        f"round-trip blocs ≠ origine :\n{m.get('blocks')}"


def t_api_unstructured():
    g = json.loads(json.dumps(_mid["nodal"]))
    g["nodes"].append({"id": "e2", "type": "entry", "params": {"mode": "manual"},
                       "x": 0, "y": 400})
    g["edges"].append({"from": "e2", "port": 0, "to": g["nodes"][1]["id"]})
    r = client.patch(f"/api/projects/{PID}/macros/{_mid['p']}",
                     json={"graph": g})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["macro"]["structured"] is False, "2 entrées = avancé attendu"
    r = client.get(f"/api/projects/{PID}/macros/{_mid['p']}?as=blocks")
    assert "blocks" not in r.get_json()["macro"], "pas de forme blocs pour un graphe avancé"


def t_api_invalid_graph():
    r = client.patch(f"/api/projects/{PID}/macros/{_mid['p']}",
                     json={"graph": {"format": "nodes/v2",
                                     "nodes": [{"id": "a", "type": "entry", "params": {}}],
                                     "edges": [{"from": "a", "port": 0, "to": "zz"}]}})
    assert r.status_code == 400, "arête vers un nœud inconnu devrait être refusée"


def t_api_system_cycle():
    r = client.post("/api/macros", json={"name": "scn-sys-test",
                                         "graph": {"format": "blocks/v1", "steps": BLOCKS}})
    assert r.status_code == 200, r.get_data(as_text=True)
    mid = r.get_json()["macro"]["id"]
    _mid["s"] = mid
    r = client.get(f"/api/macros/{mid}?as=graph")
    g = r.get_json()["macro"]["nodal"]
    assert g["format"] == "nodes/v2"
    r = client.patch(f"/api/macros/{mid}", json={"graph": g})
    assert r.status_code == 200
    assert r.get_json()["macro"]["format"] == "nodes/v2"
    r = client.get("/api/macros/catalog")
    assert r.status_code == 200 and "containers" in r.get_json()


def t_cleanup():
    r = client.delete(f"/api/projects/{PID}/macros/{_mid['p']}")
    assert r.status_code == 200
    if _mid.get("s"):
        r = client.delete(f"/api/macros/{_mid['s']}")
        assert r.status_code == 200


if __name__ == "__main__":
    for fn in (t_page_renders, t_inline_js_syntax, t_api_create_blocks, t_api_as_graph,
               t_api_patch_nodes_v2, t_api_as_blocks_roundtrip, t_api_unstructured,
               t_api_invalid_graph, t_api_system_cycle, t_cleanup):
        check(fn.__name__, fn)
    shutil.rmtree(_tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"{len(FAILS)} échec(s) : {FAILS}")
        sys.exit(1)
    print("Éditeur Scénario : tests offline OK.")
    sys.exit(0)
