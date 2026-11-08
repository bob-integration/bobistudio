#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Is the project guaranteed — and does the guarantee travel with the project?

Two holes, found 2026-11-07 while stepping back on « how does a user guarantee a project »:
  · no screen answered the question: reservations were made by hand, through the API;
  · a restore rebuilt every container from its MANIFEST: the snapshot kept neither the dedicated
    cores, nor the memory, nor the CPU weight set for that container. A guaranteed project came
    back unguaranteed, and said nothing.

What this guard holds, offline (throwaway database; creation, deploy and host doubled):
  1. the snapshot carries the per-container resources, and only when some are set (a project
     without overrides keeps a byte-identical snapshot — the living-project re-snapshot compares JSON);
  2. ★ a restore writes them on the new container BEFORE its first deploy;
  3. ★ the capacity pre-flight counts the reserved cores of the snapshot;
  4. ★ the project verdict: guaranteed only when every member has dedicated cores covering the
     peak measured on its node; otherwise one state per container, in plain words;
  5. a node with no core pool is named;
  6. ★ only containers at the CPU weight level « Dédié » must be guaranteed (Cyril, 2026-11-07):
     the others run best effort and do not count; none on air → « nothing required »;

MUTATIONS, PROJET_GARANTI_MUTATION=<name> — each must give rc=1:
    snapshot_muet     the snapshot no longer carries the resources
    restore_muet      the restore no longer applies them
    prevol_muet       the pre-flight ignores the reserved cores
    garanti_naif      « reserved » is enough to be called guaranteed (no measurement needed)
    antenne_ignoree   every container must be guaranteed, whatever its level

    ./venv/bin/python tests/verif_projet_garanti.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="projet_garanti_db_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

from app import projects, docker_compute, etalonnage, core_pool, project_resources   # noqa: E402

MUT = os.environ.get("PROJET_GARANTI_MUTATION") or ""
MUTATIONS = {
    "snapshot_muet": (database, "        if _res:\n            c[\"resources\"] = _res\n", ""),
    "restore_muet": (projects, "            db_set_resources_override(vmid, cores=_res.get(\"cores\"), memory=_res.get(\"memory\"),\n"
                               "                                      cpu_weight=_lvl)\n",
                     "            pass\n"),
    "prevol_muet": (projects, '                if (_res.get("cpu_mode") == "reserved" or _res.get("cpu_weight") == "dedicated") \\\n'
                              '                        and _res.get("cores"):',
                    '                if False:'),
    "antenne_ignoree": (project_resources, '        r["required"] = (r.get("level") == "dedicated") and not r["node_io"]', '        r["required"] = True'),
    "garanti_naif": (project_resources, '    if row.get("etat") != "etalonne" or row.get("besoin_pct") is None:\n        return "reserved_unmeasured"\n', ""),
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
    exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    print("MUTATION active :", MUT)

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)

NA = database.db_add_node("z8", "127.0.0.1")
database.db_update_node(NA, compute_cpuset="0-11")
NB = database.db_add_node("sans-pool", "127.0.0.2")


def _cont(vmid, host, typ, node, status="running"):
    database.db_upsert_container(vmid, host, 1, 512, status)
    database.get_db().execute("UPDATE containers SET node_id=? WHERE vmid=?", (node, vmid))
    database.get_db().commit()
    database.db_update_deploy_config(vmid, typ, {})


print("\n1. Le snapshot porte les ressources du conteneur")
_cont(401, "mix", "mixer", NA)
_cont(402, "cc", "color_corrector", NA)
database.db_set_resources_override(401, cores=2, cpu_mode="reserved", cpu_weight="dedicated")
snap = {c["vmid"]: c for c in database.db_snapshot_for_vmids([401, 402])}
# 2026-11-07 : « réservé » EST le niveau Dédié (la place suit le niveau) — le snapshot ne porte
# plus de mode, seulement les cœurs et le niveau.
ok(snap[401].get("resources") == {"cores": 2, "cpu_weight": "dedicated"},
   "1a. ★ cœurs et niveau Dédié posés sur le conteneur → dans le snapshot", snap[401].get("resources"))
ok("resources" not in snap[402], "1b. sans réglage propre : aucune clé (snapshot inchangé)", snap[402].keys())
ok(not any(k.endswith("_override") for k in snap[401]), "1c. pas de colonne brute `*_override` recopiée",
   [k for k in snap[401] if k.endswith("_override")])

print("\n2. ★ La restauration les remet AVANT le premier déploiement")
pid = database.get_db().execute("INSERT INTO projects (name, snapshot) VALUES ('Atelier', '[]')").lastrowid
database.get_db().commit()
database.db_update_project_snapshot(pid, [snap[401], snap[402]])
database.get_db().execute("UPDATE projects SET media_path=? WHERE id=?", (tempfile.mkdtemp(), pid))
database.get_db().commit()
_next = [501]
_vu_au_deploiement = {}


def _fake_create(node_id, deploy_type, hostname=None):
    v = _next[0]
    _next[0] += 1
    database.db_upsert_container(v, hostname, 1, 512, "running")
    return v


def _spy_deploy(vmid, typ, params, *a, **k):
    c = database.db_get_container(vmid) or {}
    _vu_au_deploiement[vmid] = (c.get("cores_override"), c.get("cpu_mode_override"), c.get("cpu_weight_override"))
    return True


docker_compute.pick_compute_node = lambda *a, **k: NA
docker_compute.creer_container_compute = _fake_create
projects.deployer_script = _spy_deploy
projects.planifier_restore = lambda snapshot: {"can_restore": True, "remaps": [],
                                               "hostname_conflicts": [], "shm_conflicts": []}
res = projects.restaurer_projet(pid)
ok(res.get("ok") == 2, "2a. décor : les deux conteneurs recréés", res)
v_mix = next((v for v in (501, 502) if (database.db_get_container(v) or {}).get("hostname", "").endswith("mix")), None)
ok(_vu_au_deploiement.get(v_mix) == (2, None, "dedicated"),
   "2b. ★★ le conteneur recréé a déjà ses 2 cœurs réservés et son poids quand il est déployé",
   _vu_au_deploiement.get(v_mix))
v_cc = 502 if v_mix == 501 else 501
ok(_vu_au_deploiement.get(v_cc) == (None, None, None), "2c. l'autre reste sur son manifeste",
   _vu_au_deploiement.get(v_cc))

print("\n3. ★ Le pré-vol compte les cœurs réservés du snapshot")
issues = projects.verifier_capacite([dict(snap[401], resources={"cores": 40, "cpu_mode": "reserved"})])
cores = [i for i in issues if i.get("kind") == "cores"]
ok(cores and cores[0]["need"] >= 40 and cores[0]["mesures"].get("mixer", {}).get("source") == "reserve",
   "3a. ★ 40 cœurs réservés sur un pool de 12 → refus, source « reserve »", cores)
issues = projects.verifier_capacite([dict(snap[401], resources={"cores": 40, "cpu_weight": "dedicated"})])
cores = [i for i in issues if i.get("kind") == "cores"]
ok(cores and cores[0]["need"] >= 40 and cores[0]["mesures"].get("mixer", {}).get("source") == "reserve",
   "3b. ★ pareil pour un snapshot récent, qui dit « Dédié » au lieu de « réservé »", cores)

print("\n4. ★ Verdict du projet")
E = project_resources.guarantee_state
ok(E({"reserve_pct": None}) == "shared", "4a. aucun cœur dédié → partagé")
ok(E({"reserve_pct": 200, "etat": "perime", "besoin_pct": 100}) == "reserved_unmeasured",
   "4b. ★ réservé mais mesure périmée → « réservé, non mesuré ici », jamais « garanti »")
ok(E({"reserve_pct": 200, "etat": "autre_noeud", "besoin_pct": None}) == "reserved_unmeasured",
   "4c. ★ mesuré sur un autre nœud → pas garanti ici")
ok(E({"reserve_pct": 200, "etat": "etalonne", "besoin_pct": 300}) == "undersized",
   "4d. pic mesuré (avec la marge) au-delà de la réserve → sous-dimensionné")
ok(E({"reserve_pct": 200, "etat": "etalonne", "besoin_pct": 200}) == "guaranteed", "4e. réserve qui couvre → garanti")

print("\n6. ★ Seuls les conteneurs au niveau Dédié doivent être garantis (choix du 07/11)")
pid2 = database.get_db().execute("INSERT INTO projects (name, snapshot) VALUES ('Régie', '[]')").lastrowid
database.get_db().commit()
for v, h, n, st in ((601, "a", NA, "running"), (602, "b", NB, "running"), (603, "c", NA, "stopped"),
                    (604, "d", NA, "running")):
    _cont(v, h, "color_corrector", n, st)
    database.db_set_project(v, pid2)
_G = {"reserve_pct": 200, "etat": "etalonne", "besoin_pct": 100, "mesure": {"max": 80}}
_lignes = {601: dict(_G, vmid=601, hostname="a", node_id=NA, level="dedicated"),
           602: {"vmid": 602, "hostname": "b", "node_id": NB, "reserve_pct": None, "etat": "non_etalonne", "level": "dedicated"},
           604: {"vmid": 604, "hostname": "d", "node_id": NA, "reserve_pct": None, "etat": "non_etalonne", "level": "normal"}}
etalonnage.ressources = lambda vmids=None, **k: [r for v, r in _lignes.items() if vmids is None or v in vmids]
rp = project_resources.project_guarantee(pid2)
etats = {c["vmid"]: (c["guarantee"], c["required"]) for c in rp["containers"]}
ok(etats == {601: ("guaranteed", True), 602: ("shared", True), 603: ("stopped", False), 604: ("shared", False)},
   "6a. un état par conteneur, et qui est à garantir (Dédié)", etats)
ok(rp["verdict"] == "not_guaranteed" and rp["counts"]["guaranteed"] == 1 and rp["counts"]["shared"] == 1
   and rp["best_effort"] == 2, "6b. ★ un conteneur Dédié non garanti → projet non garanti ; les 2 autres au mieux",
   (rp["verdict"], rp["counts"], rp["best_effort"]))
ok([c["vmid"] for c in rp["containers"]][:2] == [601, 602], "6c. les conteneurs à garantir viennent en tête",
   [c["vmid"] for c in rp["containers"]])
ok([n["id"] for n in rp["nodes_without_pool"]] == [NB], "6d. le nœud sans pool d'un conteneur Dédié est nommé",
   rp["nodes_without_pool"])

_lignes[602] = dict(_G, vmid=602, hostname="b", node_id=NB, level="dedicated")
ok(project_resources.project_guarantee(pid2)["verdict"] == "guaranteed",
   "6e. ★ tous les conteneurs Dédié garantis → projet garanti, même avec des « au mieux » partagés")
database.db_set_resources_override(603, cpu_weight="dedicated")
ok(project_resources.project_guarantee(pid2)["verdict"] == "not_guaranteed",
   "6f. un conteneur Dédié ARRÊTÉ rend le projet non garanti")
database.db_set_resources_override(603, cpu_weight="")
for v in (601, 602):
    _lignes[v]["level"] = "normal"
ok(project_resources.project_guarantee(pid2)["verdict"] == "no_dedicated",
   "6g. aucun conteneur Dédié → « rien n'est exigé », jamais « garanti »")

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — le projet dit s'il est garanti, et sa garantie voyage avec lui")
