# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Tests OFFLINE du moteur nodal nodes/v2 (9e passe ch.6) + non-régression blocks/v1.

Sans pytest (comme tests/smoke_test.py) :
    ./venv/bin/python tests/test_macros_nodal.py

Contraintes respectées :
  - DB de prod copiée dans un fichier temporaire (jamais modifiée) ;
  - AUCUN accès réseau : exec_action/exec_config/exec_post monkey-patchés (recorder) ;
  - aucun container live touché, aucun deploy.

Couverture :
  1. validate_graph (ids dupliqués, arête orpheline, port invalide, entrée absente) ;
  2. round-trip blocks→graph→blocks sur TOUTES les macros blocks/v1 de la DB (copie)
     + cas synthétiques (if imbriqué, parallel, if suivi de parallel → join any inséré) ;
  3. graph_is_structured : faux pour choice, entrées multiples, entry trigger, cycle,
     saut entre branches ;
  4. exécution nodale complète : entry → action mockée → cond → fan-out 3 branches →
     join all → set_var (le nœud aval ne s'exécute qu'UNE fois) ;
  5. join any : premier arrivé passe, l'autre jeton meurt ;
  6. boucle bornée (NODE_VISITS_MAX) → erreur journalisée ;
  7. annulation d'un run nodal en cours (sleep long) ;
  8. choice : routage par branche + défaut ;
  9. entry_id : ne démarre que l'entrée demandée ; entries trigger ignorées en manuel ;
 10. active_nodes exposé dans le snapshot pendant le run ;
 11. non-régression blocks/v1 : sleep/set_var/if/parallel/wait + action mockée,
     et macro blocks → sous-macro nodes/v2 (profondeur partagée).
"""
import os
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# DB temporaire = copie de la prod, AVANT tout usage de get_db().
_tmp = tempfile.mkdtemp(prefix="bobi_macros_test_")
_db_copy = os.path.join(_tmp, "db.sqlite")
shutil.copy2(os.path.join(ROOT, "db_bobistudio.db"), _db_copy)

from app import database  # noqa: E402
database.DB_PATH = _db_copy

from app import macros as M  # noqa: E402
from app.database import (db_create_macro, db_get_macro, db_project_macros,  # noqa: E402
                          db_system_macros, db_project_vars, db_set_project_var,
                          get_db)

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception:
        FAILS.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc()


def wait_run(run, timeout=10.0):
    deadline = time.monotonic() + timeout
    while run.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not run.running, "run toujours en cours après timeout"
    return run


def norm(steps):
    """Normalisation pour la comparaison round-trip : then/else/branches absents → []."""
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


def _pid():
    with get_db() as db:
        r = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if r:
            return r["id"]
        db.execute("INSERT INTO projects (name) VALUES ('test')")
        db.commit()
        return db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]


PID = _pid()

# ─── Mocks réseau : recorder d'actions (aucun HTTP) ───────────
CALLS = []


def _mock_exec_action(vmid, action_id, params, variables=None):
    CALLS.append(("action", vmid, action_id))
    return True


M.exec_action = _mock_exec_action
M.exec_config = lambda *a, **k: CALLS.append(("config",)) or True
M.exec_post = lambda *a, **k: CALLS.append(("post",)) or True


def run_graph_macro(graph, name="t", entry_id=None):
    mid = db_create_macro(PID, name, None, graph=graph)
    run, err = M.run_macro(mid, user="test", entry_id=entry_id)
    assert err is None, err
    return wait_run(run)


# ─── 1. validate_graph ────────────────────────────────────────

def t_validate():
    g = {"format": "nodes/v2",
         "nodes": [{"id": "e", "type": "entry", "params": {}},
                   {"id": "a", "type": "sleep", "params": {"ms": 1}},
                   {"id": "a", "type": "sleep", "params": {"ms": 1}}],
         "edges": [{"from": "e", "port": 0, "to": "zz"},
                   {"from": "a", "port": 3, "to": "e"}]}
    errs = M.validate_graph(g)
    assert any("dupliqué" in e for e in errs), errs
    assert any("inconnu : zz" in e for e in errs), errs
    assert any("port" in e for e in errs), errs
    assert M.validate_graph({"format": "nodes/v2", "nodes": [], "edges": []}) \
        == ["aucune entrée (nœud entry)"]
    assert M.validate_graph({"format": "blocks/v1"})  # mauvais format
    ok = {"format": "nodes/v2",
          "nodes": [{"id": "e", "type": "entry", "params": {}},
                    {"id": "c", "type": "cond", "params": {"cond": {}}}],
          "edges": [{"from": "e", "port": 0, "to": "c"},
                    {"from": "c", "port": 1, "to": "e"}]}
    assert M.validate_graph(ok) == [], M.validate_graph(ok)


# ─── 2. round-trip blocks ↔ graph ─────────────────────────────

def t_roundtrip_synthetic():
    cases = [
        [],
        [{"type": "sleep", "ms": 5}],
        [{"type": "action", "vmid": 200, "action_id": "take", "params": {}},
         {"type": "if", "cond": {"op": "=="},
          "then": [{"type": "set_var", "name": "a", "value": "1"},
                   {"type": "if", "cond": {"op": "!="},
                    "then": [{"type": "sleep", "ms": 1}], "else": []}],
          "else": [{"type": "post", "vmid": 200, "endpoint": "/x", "params": {}}]},
         {"type": "sleep", "ms": 2}],
        [{"type": "parallel", "branches": [[{"type": "sleep", "ms": 1}], [],
                                           [{"type": "set_var", "name": "b", "value": "2"}]]},
         {"type": "macro", "macro_id": 1}],
        # if suivi d'un parallel → insertion d'un join « any » (transparent au retour)
        [{"type": "if", "cond": {}, "then": [{"type": "sleep", "ms": 1}],
          "else": [{"type": "sleep", "ms": 2}]},
         {"type": "parallel", "branches": [[{"type": "sleep", "ms": 3}],
                                           [{"type": "sleep", "ms": 4}]]},
         {"type": "config", "vmid": 200, "params": {"k": "v"}}],
        # parallel dont une branche contient un if
        [{"type": "parallel", "branches": [
            [{"type": "if", "cond": {}, "then": [{"type": "sleep", "ms": 1}], "else": []}],
            [{"type": "wait", "cond": {}, "timeout_ms": 100}]]}],
    ]
    for i, b in enumerate(cases):
        g = M.blocks_to_graph(b)
        assert M.validate_graph(g) == [], (i, M.validate_graph(g))
        assert M.graph_is_structured(g), (i, "devrait être structuré")
        b2 = M.graph_to_blocks(g)
        assert b2 == norm(b), (i, b2, norm(b))


def t_roundtrip_db():
    macros, n = [], 0
    with get_db() as db:
        pids = [r["id"] for r in db.execute("SELECT id FROM projects").fetchall()]
    for pid in pids:
        macros += db_project_macros(pid)
    macros += db_system_macros()
    for m in macros:
        if M.macro_format(m) != "blocks/v1":
            continue
        steps = (m.get("graph") or {}).get("steps") or []
        g = M.blocks_to_graph(steps)
        assert M.validate_graph(g) == [], (m["id"], M.validate_graph(g))
        assert M.graph_is_structured(g), (m["id"], "non structuré ?")
        assert M.graph_to_blocks(g) == norm(steps), m["id"]
        n += 1
    print(f"      ({n} macros blocks/v1 de la DB, round-trip exact)")


# ─── 3. détection « avancé » ──────────────────────────────────

def t_unstructured():
    def g(nodes, edges):
        return {"format": "nodes/v2", "nodes": nodes, "edges": edges}
    E = {"id": "e", "type": "entry", "params": {}}
    # choice → nodal only
    assert not M.graph_is_structured(g(
        [E, {"id": "c", "type": "choice", "params": {"branches": [{}]}}],
        [{"from": "e", "port": 0, "to": "c"}]))
    # deux entrées
    assert not M.graph_is_structured(g(
        [E, {"id": "e2", "type": "entry", "params": {}},
         {"id": "s", "type": "sleep", "params": {"ms": 1}}],
        [{"from": "e", "port": 0, "to": "s"}, {"from": "e2", "port": 0, "to": "s"}]))
    # entrée trigger
    assert not M.graph_is_structured(g(
        [{"id": "e", "type": "entry", "params": {"mode": "trigger"}}], []))
    # cycle
    assert not M.graph_is_structured(g(
        [E, {"id": "s", "type": "sleep", "params": {"ms": 1}}],
        [{"from": "e", "port": 0, "to": "s"}, {"from": "s", "port": 0, "to": "s"}]))
    # saut entre branches d'un fan-out (pas de jointure)
    assert not M.graph_is_structured(g(
        [E, {"id": "a", "type": "sleep", "params": {"ms": 1}},
         {"id": "b", "type": "sleep", "params": {"ms": 1}}],
        [{"from": "e", "port": 0, "to": "a"}, {"from": "e", "port": 0, "to": "b"},
         {"from": "a", "port": 0, "to": "b"}]))
    # mais un graphe structuré « manuel » (sans passer par blocks_to_graph) est reconnu
    assert M.graph_is_structured(g(
        [E, {"id": "s", "type": "sleep", "params": {"ms": 1}}],
        [{"from": "e", "port": 0, "to": "s"}]))


# ─── 4-10. exécution nodale ───────────────────────────────────

def _events(run, node, event):
    return [j for j in run.journal if j.get("node") == node and j.get("event") == event]


def t_run_fanout_join_all():
    """entry → action → cond(vrai) → fan-out 3 actions → join all → set_var."""
    db_set_project_var(PID, "x", "1")
    CALLS.clear()
    g = {"format": "nodes/v2", "nodes": [
        {"id": "e", "type": "entry", "params": {}},
        {"id": "a0", "type": "action", "params": {"vmid": 200, "action_id": "prep"}},
        {"id": "c", "type": "cond", "params": {"cond": {
            "left": {"kind": "var", "name": "x"}, "op": "==",
            "right": {"kind": "const", "value": "1"}}}},
        {"id": "b1", "type": "action", "params": {"vmid": 201, "action_id": "b1"}},
        {"id": "b2", "type": "action", "params": {"vmid": 202, "action_id": "b2"}},
        {"id": "b3", "type": "action", "params": {"vmid": 203, "action_id": "b3"}},
        {"id": "j", "type": "join", "params": {"mode": "all"}},
        {"id": "v", "type": "set_var", "params": {"name": "done", "value": "yes"}},
        {"id": "no", "type": "set_var", "params": {"name": "done", "value": "NO"}},
    ], "edges": [
        {"from": "e", "port": 0, "to": "a0"},
        {"from": "a0", "port": 0, "to": "c"},
        {"from": "c", "port": 0, "to": "b1"},
        {"from": "c", "port": 0, "to": "b2"},
        {"from": "c", "port": 0, "to": "b3"},
        {"from": "c", "port": 1, "to": "no"},
        {"from": "b1", "port": 0, "to": "j"},
        {"from": "b2", "port": 0, "to": "j"},
        {"from": "b3", "port": 0, "to": "j"},
        {"from": "j", "port": 0, "to": "v"},
    ]}
    run = run_graph_macro(g, "fanout")
    assert run.error is None, run.error
    acts = sorted(c[2] for c in CALLS if c[0] == "action")
    assert acts == ["b1", "b2", "b3", "prep"], acts
    assert db_project_vars(PID).get("done") == "yes"
    assert len(_events(run, "v", "finished")) == 1   # la jointure n'a laissé passer qu'un jeton
    assert len(_events(run, "no", "started")) == 0   # branche fausse jamais exécutée
    assert run.snapshot()["active_nodes"] == []


def t_join_any():
    g = {"format": "nodes/v2", "nodes": [
        {"id": "e", "type": "entry", "params": {}},
        {"id": "s1", "type": "sleep", "params": {"ms": 10}},
        {"id": "s2", "type": "sleep", "params": {"ms": 250}},
        {"id": "j", "type": "join", "params": {"mode": "any"}},
        {"id": "v", "type": "set_var", "params": {"name": "winner", "value": "done"}},
    ], "edges": [
        {"from": "e", "port": 0, "to": "s1"}, {"from": "e", "port": 0, "to": "s2"},
        {"from": "s1", "port": 0, "to": "j"}, {"from": "s2", "port": 0, "to": "j"},
        {"from": "j", "port": 0, "to": "v"},
    ]}
    run = run_graph_macro(g, "any")
    assert run.error is None, run.error
    assert len(_events(run, "v", "finished")) == 1   # le 2e jeton est mort à la jointure
    assert len(_events(run, "j", "finished")) == 1


def t_cycle_bounded():
    g = {"format": "nodes/v2", "nodes": [
        {"id": "e", "type": "entry", "params": {}},
        {"id": "s", "type": "sleep", "params": {"ms": 0}},
    ], "edges": [
        {"from": "e", "port": 0, "to": "s"}, {"from": "s", "port": 0, "to": "s"},
    ]}
    run = run_graph_macro(g, "loop")
    assert run.error and "boucle" in run.error, run.error


def t_cancel():
    mid = db_create_macro(PID, "cancel", None, graph={
        "format": "nodes/v2", "nodes": [
            {"id": "e", "type": "entry", "params": {}},
            {"id": "s", "type": "sleep", "params": {"ms": 20000}},
        ], "edges": [{"from": "e", "port": 0, "to": "s"}]})
    run, err = M.run_macro(mid, user="test")
    assert err is None
    time.sleep(0.2)
    snap = M.run_status(mid)
    assert "s" in snap["active_nodes"], snap["active_nodes"]   # (10) surlignage live
    t0 = time.monotonic()
    assert M.cancel_run(mid)
    wait_run(run, timeout=3)
    assert time.monotonic() - t0 < 2.0, "annulation trop lente"
    assert run.error and "annulé" in run.error, run.error


def t_choice():
    db_set_project_var(PID, "n", "2")
    g = {"format": "nodes/v2", "nodes": [
        {"id": "e", "type": "entry", "params": {}},
        {"id": "ch", "type": "choice", "params": {"branches": [
            {"cond": {"left": {"kind": "var", "name": "n"}, "op": "==",
                      "right": {"kind": "const", "value": "1"}}},
            {"cond": {"left": {"kind": "var", "name": "n"}, "op": "==",
                      "right": {"kind": "const", "value": "2"}}}]}},
        {"id": "v1", "type": "set_var", "params": {"name": "route", "value": "un"}},
        {"id": "v2", "type": "set_var", "params": {"name": "route", "value": "deux"}},
        {"id": "vd", "type": "set_var", "params": {"name": "route", "value": "defaut"}},
    ], "edges": [
        {"from": "e", "port": 0, "to": "ch"},
        {"from": "ch", "port": 0, "to": "v1"},
        {"from": "ch", "port": 1, "to": "v2"},
        {"from": "ch", "port": 2, "to": "vd"},
    ]}
    run = run_graph_macro(g, "choice")
    assert run.error is None, run.error
    assert db_project_vars(PID).get("route") == "deux"
    db_set_project_var(PID, "n", "9")
    run = run_graph_macro(g, "choice2")
    assert db_project_vars(PID).get("route") == "defaut"


def t_entry_id_and_trigger_entries():
    g = {"format": "nodes/v2", "nodes": [
        {"id": "e1", "type": "entry", "params": {"mode": "manual"}},
        {"id": "e2", "type": "entry", "params": {"mode": "manual"}},
        {"id": "et", "type": "entry", "params": {"mode": "trigger"}},
        {"id": "v1", "type": "set_var", "params": {"name": "who", "value": "un"}},
        {"id": "v2", "type": "set_var", "params": {"name": "who", "value": "deux"}},
        {"id": "vt", "type": "set_var", "params": {"name": "who", "value": "trig"}},
    ], "edges": [
        {"from": "e1", "port": 0, "to": "v1"},
        {"from": "e2", "port": 0, "to": "v2"},
        {"from": "et", "port": 0, "to": "vt"},
    ]}
    run = run_graph_macro(g, "entries", entry_id="e2")
    assert run.error is None, run.error
    assert db_project_vars(PID).get("who") == "deux"
    assert len(_events(run, "v1", "started")) == 0
    # sans entry_id : toutes les manual démarrent, l'entrée trigger reste inerte
    run = run_graph_macro(g, "entries2")
    assert len(_events(run, "vt", "started")) == 0
    assert len(_events(run, "v1", "started")) == 1
    assert len(_events(run, "v2", "started")) == 1
    # graphe SANS entrée manuelle → erreur explicite
    g2 = {"format": "nodes/v2",
          "nodes": [{"id": "et", "type": "entry", "params": {"mode": "trigger"}}],
          "edges": []}
    run = run_graph_macro(g2, "trigonly")
    assert run.error and "manuelle" in run.error, run.error


# ─── 11. non-régression blocks/v1 ─────────────────────────────

def t_blocks_regression():
    CALLS.clear()
    db_set_project_var(PID, "go", "0")
    steps = [
        {"type": "set_var", "name": "b", "value": "avant"},
        {"type": "action", "vmid": 200, "action_id": "take", "params": {}},
        {"type": "if",
         "cond": {"left": {"kind": "var", "name": "b"}, "op": "==",
                  "right": {"kind": "const", "value": "avant"}},
         "then": [{"type": "set_var", "name": "b", "value": "alors"}],
         "else": [{"type": "set_var", "name": "b", "value": "sinon"}]},
        {"type": "parallel", "branches": [
            [{"type": "sleep", "ms": 150}],
            [{"type": "sleep", "ms": 150}],
            [{"type": "set_var", "name": "go", "value": "1"}]]},
        {"type": "wait", "cond": {"left": {"kind": "var", "name": "go"}, "op": "==",
                                  "right": {"kind": "const", "value": "1"}},
         "timeout_ms": 2000},
    ]
    mid = db_create_macro(PID, "blocks-reg", None,
                          graph={"format": "blocks/v1", "steps": steps})
    t0 = time.monotonic()
    run, err = M.run_macro(mid, user="test")
    assert err is None
    wait_run(run)
    dt = time.monotonic() - t0
    assert run.error is None, run.error
    assert db_project_vars(PID).get("b") == "alors"
    assert ("action", 200, "take") in CALLS
    assert dt < 1.0, f"branches parallel non concurrentes ? ({dt:.2f}s)"
    assert run.journal[-1]["msg"] == "terminé"
    assert run.snapshot()["active_nodes"] == []   # champ présent, vide en blocks


def t_blocks_calls_nodal_submacro():
    """Étape macro d'un blocks/v1 → sous-macro nodes/v2 (profondeur partagée)."""
    sub = db_create_macro(PID, "sub-nodal", None, graph={
        "format": "nodes/v2", "nodes": [
            {"id": "e", "type": "entry", "params": {}},
            {"id": "v", "type": "set_var", "params": {"name": "sub", "value": "ok"}},
        ], "edges": [{"from": "e", "port": 0, "to": "v"}]})
    mid = db_create_macro(PID, "blocks-call-nodal", None, graph={
        "format": "blocks/v1", "steps": [{"type": "macro", "macro_id": sub}]})
    run, err = M.run_macro(mid, user="test")
    assert err is None
    wait_run(run)
    assert run.error is None, run.error
    assert db_project_vars(PID).get("sub") == "ok"
    # profondeur : nodal → nodal auto-récursif borné par MAX_DEPTH
    rec = db_create_macro(PID, "rec-nodal", None, graph={"format": "blocks/v1", "steps": []})
    database.db_update_macro(rec, graph={
        "format": "nodes/v2", "nodes": [
            {"id": "e", "type": "entry", "params": {}},
            {"id": "m", "type": "macro", "params": {"macro_id": rec}},
        ], "edges": [{"from": "e", "port": 0, "to": "m"}]})
    run, err = M.run_macro(rec, user="test")
    assert err is None
    wait_run(run)
    assert run.error and "imbrication" in run.error, run.error


if __name__ == "__main__":
    for name, fn in [
        ("validate_graph", t_validate),
        ("round-trip synthétique", t_roundtrip_synthetic),
        ("round-trip macros de la DB", t_roundtrip_db),
        ("détection avancé/structuré", t_unstructured),
        ("run nodal fan-out + join all", t_run_fanout_join_all),
        ("join any", t_join_any),
        ("boucle bornée", t_cycle_bounded),
        ("annulation + active_nodes", t_cancel),
        ("choice", t_choice),
        ("entry_id + entrées trigger", t_entry_id_and_trigger_entries),
        ("non-régression blocks/v1", t_blocks_regression),
        ("blocks → sous-macro nodale + profondeur", t_blocks_calls_nodal_submacro),
    ]:
        check(name, fn)
    shutil.rmtree(_tmp, ignore_errors=True)
    if FAILS:
        print(f"\n{len(FAILS)} échec(s) : {FAILS}")
        sys.exit(1)
    print("\nTous les tests passent.")
