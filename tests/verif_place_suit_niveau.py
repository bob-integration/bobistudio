#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Guard: the place follows the level — « Dédié » means dedicated cores (decided on 2026-11-07).

Until then a container's place (dedicated / shared) and its CPU weight level were two settings,
and a type got dedicated cores because its plugin author asked for them: a multiview had some, a
colour corrector on the on-air path had none. Decided with Cyril: « if the container has dedicated
cores, it is on air » — one setting, per container.

Held offline (throwaway database; the node agent is doubled):
  1. ★ the level decides the place: on air → pinned (manifest cores, 1 when it has none); normal,
     background, batch → shared; a type whose manifest pins is on air by default (nothing already
     dedicated loses its cores); node I/O keeps its cores whatever its level;
  2. ★ the old « reserved » place becomes the level on air at boot, and the place override is
     cleared; writing « reserved » / « shared » through the old API maps to the level;
  3. ★ changing the level applies the place HOT: on air reserves and sets the cpuset, leaving it
     releases and puts the container back on the shared pool;
  4. ★ no room → refused, the level is put back, nothing reserved;
  5. a stopped container is left alone (its next deployment builds its place).

MUTATIONS, PLACE_NIVEAU_MUTATION=<name> — each must give rc=1:
    pin_default_lost   a type whose manifest pins is no longer on air by default
    level_ignored      on air does not pin
    no_hot             a level change does not move the container
    refusal_kept       a refused level change keeps the new level
    migration_off      the old « reserved » is not migrated
    alias_off          the former name « onair » is no longer accepted

    ./venv/bin/python tests/verif_place_suit_niveau.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="place_niveau_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

from app import core_pool, cpu_weight, docker_compute, node_driver, plugins    # noqa: E402

MUT = os.environ.get("PLACE_NIVEAU_MUTATION") or ""
MUTATIONS = {
    "pin_default_lost": (docker_compute, '               else "dedicated" if res.get("pin") else _cw.DEFAULT)',
                         '               else _cw.DEFAULT)'),
    "level_ignored": (docker_compute, '        res.pop("priority", None)\n        res["pin"] = True\n', '        pass\n'),
    "no_hot": (cpu_weight, "        ok_place, place = apply_place_hot(vmid)", "        ok_place, place = True, None"),
    "refusal_kept": (cpu_weight, '            db_set_resources_override(vmid, cpu_weight=(before or ""))\n', ""),
    "alias_off": (cpu_weight, 'ALIASES = {"onair": "dedicated"}', 'ALIASES = {}'),
    "migration_off": (database, '''        db.execute("UPDATE containers SET cpu_weight_override='dedicated' WHERE cpu_mode_override='reserved'")\n''', ""),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _mod, _old, _new = MUTATIONS[MUT]
    _src = open(_mod.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) — la garde est périmée" % (MUT, _src.count(_old)))
        sys.exit(1)
    _keep = dict(database.__dict__) if _mod is database else None
    exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    if _mod is database:
        database.DB_PATH = _DBF                        # the re-executed module reset its path
    print("MUTATION active :", MUT)

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


HT = {c: c % 8 for c in range(16)}
core_pool.core_map_cached = lambda nid: dict(HT)
core_pool.numa_map_cached = lambda nid: {}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
core_pool.reapply_shared_after_reservation = lambda *a, **k: None
core_pool.after_cores_released = lambda *a, **k: None
EXEC = []


def _host_exec(node, cmd, timeout=None, **kw):
    EXEC.append(cmd)
    return 0, "", ""


node_driver.host_exec = _host_exec
PROFILES = {"mixer": {"cores": 3, "pin": True}, "color_corrector": {}, "streamer": {"cores": 4, "pin": False},
            "transcoder": {"priority": "batch", "reservable": True},
            "decklink_io": {"cores": 5, "pin": True, "node_io": True, "physical_cores": True}}
plugins.get = lambda t: ({"resources": dict(PROFILES[t])} if t in PROFILES else None)

NID = database.db_add_node("n", "127.0.0.1")
database.db_update_node(NID, compute_cpuset="1-7,9-15")


def _add(vmid, typ, status="running"):
    database.db_upsert_container_docker(vmid, "c%d" % vmid, NID, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, typ, {})
    with database.get_db() as db:
        db.execute("UPDATE containers SET status=? WHERE vmid=?", (status, vmid))
        db.commit()
    return database.db_get_container(vmid)


def eff(vmid):
    c = database.db_get_container(vmid)
    return docker_compute.effective_resources(docker_compute._type_of(c), c)


print("\n1. ★ The level decides the place")
_add(801, "mixer"); _add(802, "color_corrector"); _add(803, "streamer"); _add(804, "transcoder"); _add(805, "decklink_io")
e = eff(801)
ok(e["cpu_weight"] == "dedicated" and e.get("pin") and e["cores"] == 3, "1a. ★ a pinned type is on air by default, dedicated", e)
database.db_set_resources_override(801, cpu_weight="normal")
ok(not eff(801).get("pin"), "1b. ★ set to Normal → shared", eff(801))
e = eff(802)
ok(e["cpu_weight"] == "normal" and not e.get("pin"), "1c. a type without a profile: normal, shared", e)
database.db_set_resources_override(802, cpu_weight="dedicated")
e = eff(802)
ok(e.get("pin") and e["cores"] == 1, "1d. ★ set on air → one dedicated core", e)
database.db_set_resources_override(803, cpu_weight="dedicated")
ok(eff(803).get("pin") and eff(803)["cores"] == 4, "1e. on air with the manifest's cores", eff(803))
e = eff(804)
ok(e["cpu_weight"] == "batch" and not e.get("pin") and e.get("priority") == "batch", "1f. batch stays shared", e)
database.db_set_resources_override(805, cpu_weight="normal")
ok(eff(805).get("pin"), "1g. node I/O keeps its cores whatever its level", eff(805))

print("\n2. ★ The old « reserved » becomes the level on air")
_add(806, "streamer")
with database.get_db() as db:
    db.execute("UPDATE containers SET cpu_mode_override='reserved', cpu_weight_override='normal' WHERE vmid=806")
    db.commit()
database.init_db()
c = database.db_get_container(806)
ok(c.get("cpu_weight_override") == "dedicated" and c.get("cpu_mode_override") is None,
   "2a. ★ migrated at boot: on air, place override cleared", (c.get("cpu_weight_override"), c.get("cpu_mode_override")))
_add(808, "color_corrector")
with database.get_db() as db:
    db.execute("UPDATE containers SET cpu_weight_override='onair' WHERE vmid=808")
    db.commit()
database.init_db()
ok(database.db_get_container(808).get("cpu_weight_override") == "dedicated",
   "2a'. ★ the former name « onair » is renamed « dedicated » at boot")
database.db_set_resources_override(808, cpu_weight="onair")
ok(database.db_get_container(808).get("cpu_weight_override") == "dedicated" and eff(808).get("pin")
   and cpu_weight.niveau_valide("onair"),
   "2a''. ★ « onair » is still accepted on input (saved projects, macros) and stored « dedicated »")
database.db_set_resources_override(804, cpu_mode="reserved")
ok(database.db_get_container(804).get("cpu_weight_override") == "dedicated" and eff(804).get("pin"),
   "2b. the old API « reserved » writes the level on air")
database.db_set_resources_override(804, cpu_mode="shared")
ok(database.db_get_container(804).get("cpu_weight_override") is None and not eff(804).get("pin"),
   "2c. « shared » gives it back to its manifest (batch)")

print("\n3. ★ A level change moves the container, hot")
database.db_set_resources_override(801, cpu_weight="")      # back to the manifest: on air
for v in (801, 802, 803):
    core_pool.release_cores(v)
database.db_set_resources_override(802, cpu_weight="normal")
EXEC.clear()
r = cpu_weight.regler(802, "dedicated")
held = core_pool.allocated_for(NID, 802)
ok(r.get("ok") and held == 1, "3a. ★ normal → on air: one core reserved", (r, held))
ok(any("--cpuset-cpus" in x and "bobi-802" in x for x in EXEC), "3b. ★ and set on the running container", EXEC)
EXEC.clear()
r = cpu_weight.regler(802, "background")
ok(r.get("ok") and core_pool.allocated_for(NID, 802) == 0, "3c. ★ on air → background: its cores released", r)
ok(any("--cpuset-cpus" in x and "bobi-802" in x for x in EXEC), "3d. and it is put back on the shared pool", EXEC)

print("\n4. ★ No room → refused, level kept")
# Fill the pool: 7 physical cores (0 is outside), the mixer holds 3 → take the other 4 whole.
# (899 must exist: a reservation of a vmid absent from `containers` is purged as an orphan.)
_add(899, "streamer")
core_pool.allocate_cores(NID, 801, 3)
core_pool.allocate_cores(NID, 899, 4)
database.db_set_resources_override(803, cpu_weight="normal")    # set on air in 1e: back to shared
EXEC.clear()
r = cpu_weight.regler(803, "dedicated")
c = database.db_get_container(803)
ok(not r.get("ok") and isinstance(r.get("err"), dict) and r["err"].get("error") == "no_room",
   "4a. ★ refused, saying why", r)
ok((c.get("cpu_weight_override") or "") in ("", "background", "normal") and c.get("cpu_weight_override") != "dedicated",
   "4b. ★ the level is put back as it was", c.get("cpu_weight_override"))
ok(core_pool.allocated_for(NID, 803) == 0 and not any("bobi-803" in x for x in EXEC), "4c. nothing reserved, nothing set")

print("\n5. A stopped container is left alone")
_add(807, "color_corrector", status="stopped")
EXEC.clear()
r = cpu_weight.regler(807, "dedicated")
ok(r.get("ok") and not any("--cpuset-cpus" in x for x in EXEC) and core_pool.allocated_for(NID, 807) == 0,
   "5. level written, place left to its next deployment",
   (r, EXEC))

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — la place suit le niveau : Dédié = cœurs dédiés")
