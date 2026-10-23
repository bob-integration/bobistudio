#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde : deux câbles posés à la suite ne s'effacent plus (app/routes/cabling.py, 21/10).

Le câble lisait les params en base, y posait sa modification, puis attendait le verrou du
conteneur pour redéployer. La base n'étant écrite qu'au bout de chaque redéploiement (≈ 1 min sur
decklink_io), deux câbles posés à la suite partaient de la même base, et le second redéployait
SANS le premier. Vécu en recette : l'audio câblé sur deux sorties SDI, une seule le garde.

Le test extrait `_merge_wire_change` du source (aucun import de l'application) et vérifie :
  1. le scénario de recette : deux ports d'une même liste, le second geste lu AVANT l'écriture
     du premier → les deux câbles survivent ;
  2. une clé simple changée par le geste est reprise, une clé changée ailleurs est gardée ;
  3. une clé retirée par le geste est retirée, sauf si quelqu'un l'a modifiée depuis ;
  4. une liste dont la forme a changé entre-temps reprend la valeur du geste (comportement d'avant) ;
  5. statique : les QUATRE redéploiements asynchrones (câble, câble par crochet, décâblage ×2)
     passent par `_fresh_wire_params`, et aucun ne redéploie plus les params du geste tels quels.
"""
import ast
import copy
import os
import re
import sys

SRC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "routes", "cabling.py")
SRC = open(SRC_PATH, encoding="utf-8").read()
tree = ast.parse(SRC)
fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_merge_wire_change"]
assert fn, "_merge_wire_change introuvable"
g = {}
exec(compile(ast.Module(body=fn, type_ignores=[]), "cabling_merge", "exec"), g)
merge = g["_merge_wire_change"]

fails = []


def check(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


def wire(params, slot, flow):
    """Ce que fait la branche from_list : copie la liste, change UNE entrée."""
    p = dict(params)
    lst = list(p["audio_out_ports"])
    e = dict(lst[slot]); e["flow"] = flow
    lst[slot] = e
    p["audio_out_ports"] = lst
    return p


base = {"outputs": [{"persistent_id": 1}, {"persistent_id": 2}],
        "audio_out_ports": [{"persistent_id": 1, "flow": ""}, {"persistent_id": 2, "flow": ""}],
        "layouts": {}}

# 1. Câble A sur le port 0, câble B sur le port 1, B lu AVANT que A soit en base.
orig_a = copy.deepcopy(base); new_a = wire(orig_a, 0, "player_audio")
orig_b = copy.deepcopy(base); new_b = wire(orig_b, 1, "player-2_audio")
db = merge(orig_a, new_a, base)              # A passe en premier : la base contient A
db = merge(orig_b, new_b, db)                # B à son tour, sur la base À JOUR
flows = [e["flow"] for e in db["audio_out_ports"]]
check(flows == ["player_audio", "player-2_audio"],
      "1. deux câbles sur la même liste : les deux survivent — %s" % flows)
# Contre-épreuve : l'ancien comportement (redéployer new_b tel quel) perdait A.
check([e["flow"] for e in new_b["audio_out_ports"]][0] == "",
      "1b. sans fusion, le second câble effaçait bien le premier (le test distingue)")

# 2. Clé simple.
orig = {"shm_name": "a", "audio_shm": "x"}
new = {"shm_name": "b", "audio_shm": "x"}
fresh = {"shm_name": "a", "audio_shm": "y", "autre": 1}
out = merge(orig, new, fresh)
check(out == {"shm_name": "b", "audio_shm": "y", "autre": 1},
      "2. clé du geste reprise, clés changées ailleurs gardées — %s" % out)

# 3. Retrait.
out = merge({"k": 1, "z": 2}, {"z": 2}, {"k": 1, "z": 2})
check("k" not in out, "3. clé retirée par le geste : retirée")
out = merge({"k": 1}, {}, {"k": 5})
check(out.get("k") == 5, "3b. clé retirée par le geste mais modifiée depuis : gardée")

# 4. Liste allongée entre-temps : valeur du geste.
out = merge({"l": [1, 2]}, {"l": [1, 3]}, {"l": [1, 2, 9]})
check(out["l"] == [1, 3], "4. forme changée entre-temps : la valeur du geste")

# 5. Statique.
bodies = re.findall(r"def (_async_(?:wire|unwire|deploy))\(\):\n((?:\s{8,}.*\n)+?)\s*threading",
                    SRC)
check(len(bodies) == 4, "5. quatre redéploiements asynchrones trouvés (%d)" % len(bodies))
check(all("_fresh_wire_params(to_vmid, _orig_params, params)" in b for _, b in bodies),
      "5b. tous passent par _fresh_wire_params")
check(not re.search(r"deployer_script\(to_vmid, t, params\)", SRC),
      "5c. plus aucun redéploiement des params du geste tels quels")
check(SRC.count("_orig_params = copy.deepcopy(params)") == 2,
      "5d. instantané PROFOND au geste, dans _apply_wire et _apply_unwire")

print("\n%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
