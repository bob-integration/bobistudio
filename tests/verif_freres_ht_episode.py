#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""An HT sharing is a STATE: said when it changes, not every 5 minutes (2026-11-23).

`alert.resource.dedie_frere_ht` came back 541 times in three days on dell-1 (throttled to one per
5 min, for the same twelve containers). Now: one alert when the set of containers concerned
appears or changes, nothing while it stays the same, one info when it is gone — for both cases
(sibling in the shared pool, sibling held by another dedicated). Never lost to a throttle. The
node's aptitude sheet shows the state the rest of the time.

MUTATIONS, HT_MUTATION=<name> — each must give rc=1:
    toujours    said at every pass (the old behaviour without throttle)
    jamais_fin  nothing said when it is gone

    ./venv/bin/python tests/verif_freres_ht_episode.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_ht_episode_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool                                                     # noqa: E402

MUT = os.environ.get("HT_MUTATION") or ""
MUTATIONS = {
    "toujours": ("    if _ht_episodes.get(k) == sig:\n        return False\n", ""),
    "jamais_fin": ('            db_add_alert("alert.resource.dedie_frere_ht_fin", "info", node_id=node_id, kind="resource",\n'
                   '                         params={"n": nom_noeud})\n', "            pass\n"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation:", MUT)
        sys.exit(2)
    _old, _new = MUTATIONS[MUT]
    _src = open(core_pool.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), core_pool.__file__, "exec"), core_pool.__dict__)
    print("MUTATION active:", MUT)

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


core_pool._ht_episodes.chemin = os.path.join(os.path.dirname(_TMP), "episodes.json")
core_pool._ht_episodes._d.clear()
said = []
core_pool.db_add_alert = lambda key, *a, **k: said.append(key)
core_pool.db_get_node = lambda nid: {"id": nid, "name": "ht-x"}
NID = 901


def vue(shared=(), pairs=()):
    d = [{"vmid": v, "hostname": "c%d" % v, "exclusif": False, "freres_partages": "4%d" % v,
          "cpuset_reserve": "1"} for v in shared]
    d += [{"vmid": v, "hostname": "p%d" % v, "exclusif": False, "cpuset_reserve": "2",
           "siblings_dedicated": [{"hostname": "q", "vmid": 9, "cpuset": "3", "siblings": "2/3"}]}
          for v in pairs]
    return {"ht_connu": True, "dedies": d, "partage": {"cpuset": "10-20"}}


print("\n1. Sibling in the shared pool")
said.clear()
verifie(core_pool.alerter_dedies_non_exclusifs(NID, vue((1, 2))) is True and said == ["alert.resource.dedie_frere_ht"],
        "appears: said", said)
said.clear()
for _ in range(5):
    core_pool.alerter_dedies_non_exclusifs(NID, vue((2, 1)))
verifie(said == [], "★ the same set (any order), five passes: silent", said)
core_pool.alerter_dedies_non_exclusifs(NID, vue((1, 2, 3)))
verifie(said == ["alert.resource.dedie_frere_ht"], "★ the set grows: said again, not throttled", said)
said.clear()
core_pool.alerter_dedies_non_exclusifs(NID, vue(()))
core_pool.alerter_dedies_non_exclusifs(NID, vue(()))
verifie(said == ["alert.resource.dedie_frere_ht_fin"], "★ gone: one info, once", said)

print("\n2. Sibling held by another dedicated")
said.clear()
core_pool.alerter_dedies_non_exclusifs(NID, vue(pairs=(5,)))
core_pool.alerter_dedies_non_exclusifs(NID, vue(pairs=(5,)))
verifie(said == ["alert.resource.dedicated_sibling_dedicated"], "appears once, then silent", said)
said.clear()
core_pool.alerter_dedies_non_exclusifs(NID, vue())
verifie(said == ["alert.resource.dedicated_sibling_dedicated_fin"], "gone: one info", said)
said.clear()
core_pool.alerter_dedies_non_exclusifs(NID + 1, vue((1, 2)))
verifie(said == ["alert.resource.dedie_frere_ht"], "another node has its own state")

print("\n3. Labels")
import json                                                                   # noqa: E402
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = [k for k in ("alert.resource.dedie_frere_ht_fin", "alert.resource.dedicated_sibling_dedicated_fin")
               if not cat.get(k)]
    verifie(not missing, "%s: labels" % lang, missing)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
