#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Les cœurs DÉDIÉS tournent en `performance` avec plancher au max — sur TOUT nœud, 2110 ou non.

★ LE CONSTAT (Z8, 2026-10-29 22 h). Nœud DeckLink en powersave (intel_pstate, EPP
balance_performance) : les cœurs dédiés du mélangeur (21-23) tournaient vers 1,0 GHz pour 4,0
possibles — plus lent au repos (11,1 ms) que sous charge (6,7 ms). `ensure_cpufreq_performance`
existait, mais seule la préparation MTL d'un nœud 2110 (et `node_recovery`) l'appelait, et son
plancher ne couvrait que la bande ISOLÉE.

Ce que cette garde tient, sur les VRAIES fonctions :
1. ★ le script RENDU (`mtl.CPUFREQ_SCRIPT`), exécuté sur un sysfs factice : performance partout,
   plancher max sur isolés ∪ dédiés (liste persistée lue dans un fichier), plancher RETIRÉ ailleurs ;
2. ★ la commande d'installation RÉELLE (`ensure_cpufreq_performance`) écrit la liste des dédiés
   dans un fichier persistant, que l'unité relit ;
3. ★ on MESURE avant d'agir : un nœud déjà conforme n'est pas réinstallé ;
4. la sélection des cœurs (`dedicated_floor_cores`) : réservations des conteneurs vivants ;
5. ★ une réservation sur un nœud NON 2110 déclenche la mise en performance ;
6. ★ la libération (destruction) retire le plancher des cœurs rendus ;
7. un échec n'est jamais muet (alerte).
Les C-states ne sont pas touchés (décision ultérieure, après mesure sur la Z8).

Hors ligne : base jetable ; le script et les commandes hôte RÉELS tournent dans un bac à sable
(/sys, /etc, /usr/local/sbin réécrits vers un dossier temporaire, `systemctl` factice).

MUTATIONS (CPUFREQ_DEDIES_MUTATION=<nom>) — chacune doit donner rc=1 :
    plancher_absent_dedies   le script n'épingle plus les cœurs dédiés (isolés seulement)
    plancher_non_retire      le script ne rend plus le plancher des cœurs libérés
    liste_non_persistee      la commande d'installation n'écrit plus la liste des dédiés
    toujours_reinstalle      on réinstalle sans mesurer
    non_declenche            la réservation ne déclenche plus la mise en performance
    seulement_2110           la mise en performance n'est déclenchée que sur un nœud 2110
    liberation_sans_cpufreq  la libération ne retire plus le plancher
    echec_muet               un échec n'est plus alerté
    installation_sans_performance  l'enrôlement ne met plus le nœud en performance

    ./venv/bin/python tests/verif_cpufreq_coeurs_dedies.py
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="verif_cpufreq_")
_TMP = os.path.join(_TMPDIR, "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool, mtl, node_driver, placement                        # noqa: E402
from app.routes import enrollment                                               # noqa: E402

MUT = os.environ.get("CPUFREQ_DEDIES_MUTATION") or ""
MUTATIONS = {
    "plancher_absent_dedies": (mtl, 'for p in $(echo "$ISO,$DED" | tr', 'for p in $(echo "$ISO" | tr'),
    "plancher_non_retire": (mtl, '      echo "$lo" > "$d/scaling_min_freq" 2>/dev/null || rc=1',
                            "      true"),
    "liste_non_persistee": (mtl, '    if dedicated is not None:\n        ded = (',
                            '    if False:\n        ded = ('),
    "toujours_reinstalle": (mtl, "    if conf:\n        return False, True, why",
                            "    if False:\n        return False, True, why"),
    "non_declenche": (core_pool, "    finally:\n        sync_dedicated_cpufreq(node_id, vmid)",
                      "    finally:\n        pass"),
    "seulement_2110": (core_pool, "    finally:\n        sync_dedicated_cpufreq(node_id, vmid)",
                       "    finally:\n        if isolated_cached(node_id):\n"
                       "            sync_dedicated_cpufreq(node_id, vmid)"),
    "liberation_sans_cpufreq": (core_pool, "    sync_dedicated_cpufreq(node_id, None)\n", ""),
    "installation_sans_performance": (enrollment, "        _cp.sync_dedicated_cpufreq(node_id)\n", ""),
    "echec_muet": (core_pool, "    log.warning(\"cpufreq on node %s for dedicated cores [%s] failed: %s\",",
                   "    return acted, ok, msg\n    log.warning(\"cpufreq on node %s for dedicated cores [%s] failed: %s\","),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _mod, _old, _new = MUTATIONS[MUT]
    with open(_mod.__file__, encoding="utf-8") as _f:
        _src = _f.read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) — la garde est périmée"
              % (MUT, _src.count(_old)))
        sys.exit(1)
    if _mod is enrollment:
        # Module de routes : on ne ré-exécute que la fonction mutée.
        _f = next(n for n in ast.parse(_src).body if isinstance(n, ast.FunctionDef)
                  and n.name == "_enroll_finalize")
        _src = ast.get_source_segment(_src, _f)
    exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    print("MUTATION active :", MUT)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Bac à sable : un « hôte » de 8 cœurs, powersave, plancher bas ────────────────────────────────
SB = os.path.join(_TMPDIR, "hote")
CPU = os.path.join(SB, "sys/devices/system/cpu")
MIN, MAX = 800000, 4000000
REMAP = [("/sys/devices/system/cpu", CPU),
         ("/usr/local/sbin", os.path.join(SB, "sbin")),
         ("/etc/systemd/system", os.path.join(SB, "systemd")),
         ("/etc/bobi", os.path.join(SB, "etc_bobi"))]


def _hote_neuf(isolated=""):
    shutil.rmtree(SB, ignore_errors=True)
    for d in ("sbin", "systemd", "etc_bobi", "bin"):
        os.makedirs(os.path.join(SB, d), exist_ok=True)
    for i in range(8):
        d = os.path.join(CPU, "cpu%d" % i, "cpufreq")
        os.makedirs(d)
        for k, v in (("scaling_governor", "powersave"), ("scaling_min_freq", MIN),
                     ("cpuinfo_min_freq", MIN), ("cpuinfo_max_freq", MAX)):
            with open(os.path.join(d, k), "w") as f:
                f.write("%s\n" % v)
    with open(os.path.join(CPU, "isolated"), "w") as f:
        f.write(isolated + "\n")
    # systemctl factice : l'état de l'unité dans un fichier ; `restart` joue le script réel.
    with open(os.path.join(SB, "bin", "systemctl"), "w") as f:
        f.write('#!/bin/sh\nS=%s\ncase "$1" in\n'
                '  is-enabled) cat "$S/unit_state" 2>/dev/null || echo disabled ;;\n'
                '  enable) echo enabled > "$S/unit_state" ;;\n'
                '  restart) sh "$S/sbin/bobi-cpufreq-perf.sh" >/dev/null 2>&1 ;;\n'
                '  *) : ;;\nesac\n' % SB)
    os.chmod(os.path.join(SB, "bin", "systemctl"), 0o755)


def _sh(cmd):
    for a, b in REMAP:
        cmd = cmd.replace(a, b)
    env = dict(os.environ, PATH=os.path.join(SB, "bin") + ":" + os.environ.get("PATH", ""))
    p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60, env=env,
                       cwd=SB)
    return p.returncode, p.stdout, p.stderr


def _etat():
    out = {}
    for i in range(8):
        d = os.path.join(CPU, "cpu%d" % i, "cpufreq")
        out[i] = (open(os.path.join(d, "scaling_governor")).read().strip(),
                  int(open(os.path.join(d, "scaling_min_freq")).read().strip()))
    return out


def _planchers():
    return {i for i, (_, mn) in _etat().items() if mn == MAX}


_hote = {"cmds": [], "joignable": True}
_lock = threading.Lock()


def _faux_host_exec(node, cmd, input_data=None, timeout=300, **kw):
    with _lock:
        _hote["cmds"].append(((node or {}).get("id"), cmd))
    if not _hote["joignable"]:
        return (255, "", "agent unreachable")
    if "docker update" in cmd:
        return (0, "", "")
    if "cpufreq" in cmd:
        return _sh(cmd)
    return (0, "", "")


node_driver.host_exec = _faux_host_exec
placement.releve_cache = lambda node_id, ttl=None, force=False: {"containers": []}
core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
core_pool.isolated_cached = lambda nid: set()
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)


def _attendre():
    for t in list(threading.enumerate()):
        if t.name.startswith("reapply-shared-") and t is not threading.current_thread():
            t.join(30)


def _installs():
    return [c for _, c in _hote["cmds"] if "cat > /usr/local/sbin/bobi-cpufreq-perf.sh" in c]


# ════════════════════════════════════════════════════════════════════════════════════════════
print("\n1. ★ Le script RENDU, sur un sysfs factice")
_hote_neuf(isolated="6")
with open(os.path.join(SB, "etc_bobi", "cpufreq-dedicated"), "w") as f:
    f.write("2-3\n")
_script = mtl.CPUFREQ_SCRIPT
for _a, _b in REMAP:
    _script = _script.replace(_a, _b)
with open(os.path.join(SB, "sbin", "bobi-cpufreq-perf.sh"), "w") as f:
    f.write(_script)
rc, out, err = _sh("sh /usr/local/sbin/bobi-cpufreq-perf.sh")
e = _etat()
verifie(rc == 0, "1a. le script tourne (rc=0)", (rc, out, err))
verifie(all(g == "performance" for g, _ in e.values()), "1b. performance sur TOUS les cœurs", e)
verifie(_planchers() == {2, 3, 6},
        "1c. ★★ plancher au max sur isolés (6) ∪ DÉDIÉS (2-3) — et nulle part ailleurs", e)
with open(os.path.join(SB, "etc_bobi", "cpufreq-dedicated"), "w") as f:
    f.write("\n")
_sh("sh /usr/local/sbin/bobi-cpufreq-perf.sh")
verifie(_planchers() == {6},
        "1d. ★★ liste vidée → plancher RETIRÉ des cœurs rendus (ils chaufferaient pour rien, et "
        "toute mesure future de leur comportement normal serait faussée) ; l'isolé le garde",
        _etat())
os.remove(os.path.join(SB, "etc_bobi", "cpufreq-dedicated"))
_sh("sh /usr/local/sbin/bobi-cpufreq-perf.sh")
verifie(_planchers() == {6}, "1e. fichier absent (nœud jamais réservé) : isolés seuls, sans erreur")

print("\n2. ★ La commande d'installation RÉELLE persiste la liste des dédiés")
_hote_neuf(isolated="")
_run = mtl._run_agent({"id": 0})
ok, msg, _ = mtl.ensure_cpufreq_performance("h", run=_run, dedicated="4,5")
_ded = os.path.join(SB, "etc_bobi", "cpufreq-dedicated")
verifie(ok, "2a. installée", msg)
verifie(os.path.exists(_ded) and open(_ded).read().strip() == "4,5",
        "2b. ★★ liste écrite dans un fichier PERSISTANT (l'unité la relit au boot ; un redémarrage "
        "ne doit pas remettre les cœurs dédiés au plancher bas)",
        open(_ded).read() if os.path.exists(_ded) else "absent")
verifie(_planchers() == {4, 5}, "2c. et appliquée à chaud", _etat())
ok, msg, _ = mtl.ensure_cpufreq_performance("h", run=_run)
verifie(open(_ded).read().strip() == "4,5" and _planchers() == {4, 5},
        "2d. la prép MTL / la réparation (dedicated=None) GARDENT la liste en place")

print("\n3. ★ On mesure avant d'agir")
_hote_neuf(isolated="")
_hote["cmds"].clear()
acted, ok, msg = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(acted and ok and _planchers() == {1, 2}, "3a. nœud en powersave → mis en performance",
        (acted, ok, msg, _etat()))
_hote["cmds"].clear()
acted, ok, msg = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(not acted and ok and not _installs(),
        "3b. ★ déjà conforme (unité, script, liste, gouverneurs, planchers) → RIEN n'est réinstallé",
        (acted, ok, msg))
with open(os.path.join(CPU, "cpu7/cpufreq/scaling_governor"), "w") as f:
    f.write("powersave\n")
acted, ok, msg = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(acted and ok, "3c. un cœur repassé en powersave est vu, et corrigé", msg)

print("\n4. Sélection des cœurs dédiés")
NA = database.db_add_node("z8", "127.0.0.1")       # nœud DeckLink : aucun moteur, aucune bande isolée
database.db_update_node(NA, compute_cpuset="0-7", agent_url="http://127.0.0.1:9100")


def _ajoute(vmid, nom, nid, typ="delay"):
    database.db_upsert_container_docker(vmid, nom, nid, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, typ, {})


_ajoute(6001, "melangeur", NA, typ="split")
_c = core_pool.allocate_cores(NA, 6001, 3)
with database.get_db() as _db:          # réservation orpheline (conteneur disparu)
    _db.execute("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)", (NA, 7, 6999))
    _db.commit()
_sel = core_pool.dedicated_floor_cores(NA)
verifie(_sel == sorted(core_pool.parse_cpuset(_c)),
        "4a. ★ les réservations des conteneurs VIVANTS, pas l'orpheline (elle ne porte aucune échéance)",
        (_sel, _c))
core_pool.release_cores(6999)

print("\n5. ★ Une réservation sur un nœud NON 2110 met le nœud en performance")
_hote_neuf(isolated="")
_hote["cmds"].clear()
core_pool.reapply_shared_after_reservation(NA, 6001, _c)
_attendre()
verifie(_planchers() == core_pool.parse_cpuset(_c)
        and all(g == "performance" for g, _ in _etat().values()),
        "5a. ★★ Z8 (DeckLink, sans bande isolée) : performance partout, plancher max sur les "
        "cœurs du mélangeur", _etat())

print("\n6. ★ La libération retire le plancher")
database.db_delete_container(6001)
core_pool.release_cores(6001)
_hote["cmds"].clear()
core_pool.after_cores_released(NA)
verifie(_planchers() == set() and all(g == "performance" for g, _ in _etat().values()),
        "6a. ★★ cœurs rendus : plancher retiré (le gouverneur reste performance)", _etat())
with open(os.path.join(RACINE, "app", "containers.py"), encoding="utf-8") as f:
    _arbre = ast.parse(f.read())
_cibles = [kw.value.attr for n in ast.walk(_arbre) if isinstance(n, ast.Call)
           and getattr(n.func, "attr", None) == "Thread"
           for kw in n.keywords if kw.arg == "target" and isinstance(kw.value, ast.Attribute)]
verifie("after_cores_released" in _cibles,
        "6b. la destruction lance bien `after_cores_released` en tâche de fond", _cibles)

print("\n7. Un échec n'est jamais muet")
_hote["joignable"] = False
core_pool.sync_dedicated_cpufreq(NA, 6001)
_hote["joignable"] = True
with database.get_db() as _db:
    _al = [dict(r) for r in _db.execute(
        "SELECT * FROM alerts WHERE node_id=? AND (message LIKE '%cpufreq_dedies_echec%' "
        "OR message LIKE '%NON épinglée%')", (NA,))]
verifie(len(_al) >= 1 and _al[-1].get("kind") == "resource",
        "7a. ★ nœud injoignable → alerte (fréquence non épinglée = cadence perdue en silence)", _al)

print("\n8. ★ L'INSTALLATION met tout nœud en performance (vrai _enroll_finalize, décision 30/10)")
_hote_neuf(isolated="")
from app import ca as _ca, mxl_history as _mh                                   # noqa: E402
node_driver.ping = lambda url: True
node_driver.register = lambda ip, port, token, name=None: (True, {})
enrollment._provision_shared_images = lambda node: ([], [])
core_pool.read_cpu_core_map = lambda node: {}
_ca.ca_available = lambda: False
_mh.sync_node = lambda *a, **k: (False, True, "")   # la profondeur a sa garde ; ses commandes
                                                    # viseraient le VRAI /dev/shm ici
NI = database.db_add_node("neuf", "127.0.0.9")
enrollment._enroll_finalize(NI, "127.0.0.9")
verifie(all(g == "performance" for g, _ in _etat().values()),
        "8a. ★★ nœud neuf en powersave → gouverneur performance sur TOUS les cœurs dès l'installation "
        "(Z8 : cœurs du mélangeur à 1,0 GHz, 11,1 ms contre 4,4)", _etat())
_us = os.path.join(SB, "unit_state")
verifie(os.path.exists(_us) and open(_us).read().strip() == "enabled",
        "8b. ★ unité activée : le réglage survit au redémarrage du nœud")
verifie(_planchers() == set(), "8c. aucun cœur encore dédié → aucun plancher forcé", _etat())

print("\n" + ("─" * 70))
shutil.rmtree(_TMPDIR, ignore_errors=True)
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ cœurs dédiés en performance avec plancher max, sur tout nœud ; plancher retiré à la "
      "libération ; mesure avant d'agir ; échec alerté")
