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
1. ★ le script RENDU (`mtl.cpufreq_script`), exécuté sur un sysfs factice : performance partout,
   plancher max sur isolés ∪ dédiés (liste persistée lue dans un fichier), plancher RETIRÉ ailleurs ;
2. ★ la commande d'installation RÉELLE (`ensure_cpufreq_performance`) écrit la liste des dédiés
   dans un fichier persistant, que l'unité relit ;
3. ★ on MESURE avant d'agir : un nœud déjà conforme n'est pas réinstallé ;
4. la sélection des cœurs (`dedicated_floor_cores`) : réservations des conteneurs vivants ;
5. ★ une réservation sur un nœud NON 2110 déclenche la mise en performance ;
6. ★ la libération (destruction) retire le plancher des cœurs rendus ;
7. un échec n'est jamais muet (alerte) ;
8. ★ la VEILLE PROFONDE (C-states) est coupée sur isolés ∪ dédiés et RENDUE ailleurs, par SEUIL
   de latence annoncée par le noyau — aucun nom d'état en dur ;
9. ★ la marque du script change avec ses devoirs : un nœud resté à l'ancien script est vu
   « périmé » et se réinstalle, au lieu de manquer la coupure en silence ;
10. ★ le BUDGET de réveil est un réglage PAR NŒUD (`cpuidle_latency_budget_us`) : le script rendu
    le porte, la conformité se mesure avec LE MÊME, et une valeur très grande éteint la coupure.

★ LE SECOND CONSTAT (dl360-1, banc du 2026-11-07). Cœur 16 dédié, fil SCHED_FIFO de la console
audio, blocs de 0,67 à 1 ms : C6 actif → réveils tardifs jusqu'à ~1 ms et blocs manqués ;
C1E+C6 coupés sur ce seul cœur → réveil p99.999 ~20 µs, 0 bloc manqué sur 4 × 10 min. Le coût
de sortie est annoncé par le noyau (C6 133 µs, C1E 10, C1 2), d'où le seuil
`mtl.CPUIDLE_MAX_LATENCY_US`. L'interdiction GLOBALE a été écartée (turbo, consommation).

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
    veille_non_coupee        le script ne coupe plus la veille profonde des cœurs dédiés
    veille_non_rendue        le script ne rend plus la veille des cœurs libérés
    conforme_ignore_veille   la conformité ne regarde plus la veille (un nœud à C6 passe conforme)
    marque_script_inchangee  la marque n'a pas changé : un nœud à l'ancien script passe « à jour »

    ./venv/bin/python tests/verif_cpufreq_coeurs_dedies.py
"""
import ast
import json
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
    "toujours_reinstalle": (mtl, "    if conf:\n        return False, True, why\n    fmt = ",
                            "    if False:\n        return False, True, why\n    fmt = "),
    "non_declenche": (core_pool, "    finally:\n        sync_dedicated_cpufreq(node_id, vmid)",
                      "    finally:\n        pass"),
    "seulement_2110": (core_pool, "    finally:\n        sync_dedicated_cpufreq(node_id, vmid)",
                       "    finally:\n        if isolated_cached(node_id):\n"
                       "            sync_dedicated_cpufreq(node_id, vmid)"),
    "liberation_sans_cpufreq": (core_pool, "    sync_dedicated_cpufreq(node_id, None)\n", ""),
    "installation_sans_performance": (enrollment, "        _cp.sync_dedicated_cpufreq(node_id)\n", ""),
    # Le script est un littéral DANS mtl.py : les motifs ci-dessous visent sa SOURCE (donc
    # @@CLAT@@, pas la valeur substituée).
    "veille_non_coupee": (mtl, '      [ "$lat" -gt @@CLAT@@ ] 2>/dev/null && want=1', "      true"),
    "veille_non_rendue": (mtl, '    if [ "$cur" != "$want" ]; then\n      echo "$want"',
                          '    if [ "$cur" != "$want" ] && [ "$want" = 1 ]; then\n      echo "$want"'),
    "conforme_ignore_veille": (mtl, '    for c, etats in sorted((st.get("idle") or {}).items()):',
                               "    for c, etats in []:"),
    "marque_script_inchangee": (mtl, 'CPUFREQ_SCRIPT_MARK = "bobi-cpufreq-mark: cstates-v1"',
                                'CPUFREQ_SCRIPT_MARK = "cpufreq-dedicated"'),
    "budget_ignore_au_rendu": (mtl, "    script = cpufreq_script(cpuidle_budget_us(node_id))",
                               "    script = cpufreq_script(None)"),
    "budget_ignore_a_la_mesure": (mtl, "    conf, why = cpufreq_conforme(st, ded, budget_us=budget)",
                                  "    conf, why = cpufreq_conforme(st, ded)"),
    "budget_sans_echappatoire": (mtl, '      [ "$lat" -gt @@CLAT@@ ] 2>/dev/null && want=1',
                                 '      want=1'),
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


# Latences RELEVÉES sur dl360-1 le 2026-11-07 (cpuidle/stateN/latency, µs) : POLL, C1, C1E, C6.
# Le seuil (5 µs) tombe donc entre C1 et C1E — c'est tout l'intérêt de les prendre telles quelles
# plutôt que d'inventer des valeurs rondes qui encadreraient le seuil trop confortablement.
LATENCES = (0, 2, 10, 133)
PROFONDS = {lat for lat in LATENCES if lat > 5}          # {10, 133} : C1E et C6


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
        for j, lat in enumerate(LATENCES):
            sd = os.path.join(CPU, "cpu%d" % i, "cpuidle", "state%d" % j)
            os.makedirs(sd)
            for k, v in (("latency", lat), ("disable", 0)):
                with open(os.path.join(sd, k), "w") as f:
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


def _veilles():
    """{cœur: {latences des états DÉSACTIVÉS}} — seulement les cœurs qui en ont au moins un."""
    out = {}
    for i in range(8):
        coupes = set()
        for j, lat in enumerate(LATENCES):
            sd = os.path.join(CPU, "cpu%d" % i, "cpuidle", "state%d" % j)
            if open(os.path.join(sd, "disable")).read().strip() not in ("", "0"):
                coupes.add(lat)
        if coupes:
            out[i] = coupes
    return out


def _reveille(cpu, lat):
    """Réactive un état sur un cœur, comme le ferait un redémarrage sans l'unité, un outil
    tiers ou un réglage BIOS — la dérive que la conformité doit voir."""
    j = LATENCES.index(lat)
    with open(os.path.join(CPU, "cpu%d" % cpu, "cpuidle", "state%d" % j, "disable"), "w") as f:
        f.write("0\n")


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
_script = mtl.cpufreq_script(5)
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

print("\n9. ★ La VEILLE PROFONDE du script RENDU : coupée sur isolés ∪ dédiés, rendue ailleurs")


def _pose_script():
    sc = mtl.cpufreq_script(mtl.cpuidle_budget_us())
    for _a, _b in REMAP:
        sc = sc.replace(_a, _b)
    with open(os.path.join(SB, "sbin", "bobi-cpufreq-perf.sh"), "w") as f:
        f.write(sc)


_hote_neuf(isolated="6")
with open(os.path.join(SB, "etc_bobi", "cpufreq-dedicated"), "w") as f:
    f.write("2-3\n")
_pose_script()
rc, out, err = _sh("sh /usr/local/sbin/bobi-cpufreq-perf.sh")
verifie(rc == 0, "9a. le script tourne (rc=0)", (rc, out, err))
verifie(_veilles() == {2: PROFONDS, 3: PROFONDS, 6: PROFONDS},
        "9b. ★★ veille profonde coupée sur isolés (6) ∪ DÉDIÉS (2-3) — et nulle part ailleurs",
        _veilles())
verifie(all(PROFONDS == set(v) for v in _veilles().values()),
        "9c. ★★ seuls les états AU-DESSUS du seuil sont coupés : C1 (2 µs) et POLL restent "
        "disponibles — couper tout coûterait le repos sans rien gagner sur l'échéance",
        _veilles())
with open(os.path.join(SB, "etc_bobi", "cpufreq-dedicated"), "w") as f:
    f.write("\n")
_sh("sh /usr/local/sbin/bobi-cpufreq-perf.sh")
verifie(_veilles() == {6: PROFONDS},
        "9d. ★★ liste vidée → veille RENDUE aux cœurs libérés (un cœur libéré ne reste pas "
        "éveillé pour rien) ; l'isolé garde la sienne coupée", _veilles())

print("\n10. ★ La conformité VOIT la veille : sans ça, « mesurer avant d'agir » ne corrige rien")
_hote_neuf(isolated="")
_hote["cmds"].clear()
acted, ok, msg = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(acted and ok and _veilles() == {1: PROFONDS, 2: PROFONDS},
        "10a. ★★ réservation → veille profonde coupée sur les cœurs dédiés (dl360-1 : C6 à 133 µs "
        "contre des blocs de 0,67 ms)", (acted, ok, msg, _veilles()))
_hote["cmds"].clear()
acted, ok, _m = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(not acted and ok and not _installs(), "10b. déjà conforme → rien n'est réinstallé", _m)
_reveille(1, 133)
_st = mtl.cpufreq_state(mtl._run_agent({"id": 0}))
_conf, _why = mtl.cpufreq_conforme(_st, [1, 2])
verifie(not _conf and "deep sleep" in _why,
        "10c. ★★ C6 réactivé sous nos pieds sur un cœur dédié → NON conforme, et la raison le "
        "nomme (sinon la dérive est invisible : aucun compteur de charge ne bouge)", _why)
acted, ok, _m = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(acted and ok and _veilles() == {1: PROFONDS, 2: PROFONDS},
        "10d. … et elle est recoupée", (acted, ok, _m, _veilles()))
_j = LATENCES.index(133)
with open(os.path.join(CPU, "cpu5/cpuidle/state%d/disable" % _j), "w") as f:
    f.write("1\n")
_st = mtl.cpufreq_state(mtl._run_agent({"id": 0}))
_conf, _why = mtl.cpufreq_conforme(_st, [1, 2])
verifie(not _conf and "left disabled" in _why,
        "10e. ★ et l'inverse : un état laissé coupé sur un cœur NON dédié n'est pas conforme "
        "(la consommation de toute la machine, pour personne)", _why)
mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(_veilles() == {1: PROFONDS, 2: PROFONDS}, "10f. … et il est rendu", _veilles())

print("\n11. ★ La marque du script : un nœud resté à l'ancien se réinstalle de lui-même")
# Script de la version PRÉCÉDENTE : il parle bien de `cpufreq-dedicated` (l'ancienne marque),
# mais il ignore la veille. S'il passait pour « à jour », le nœud garderait son C6 en silence —
# le pire des cas, puisque la fenêtre des cœurs afficherait un nœud conforme.
with open(os.path.join(SB, "sbin", "bobi-cpufreq-perf.sh"), "w") as f:
    f.write("#!/bin/sh\n# ancienne version : plancher de frequence seul\n"
            "cat /etc/bobi/cpufreq-dedicated >/dev/null 2>&1\nexit 0\n")
_st = mtl.cpufreq_state(mtl._run_agent({"id": 0}))
verifie(_st is not None and _st["script_ok"] is False,
        "11a. ★★ l'ancien script est vu PÉRIMÉ (la marque a changé avec le devoir)",
        (_st or {}).get("script_ok"))
_hote["cmds"].clear()
acted, ok, _m = mtl.sync_dedicated_cpufreq({"id": 0}, [1, 2])
verifie(acted and ok and _installs(), "11b. → il est réinstallé sans qu'on ait rien à demander", _m)
_st = mtl.cpufreq_state(mtl._run_agent({"id": 0}))
verifie((_st or {}).get("script_ok") is True, "11c. et il est à jour après coup")

print("\n12. La fenêtre des cœurs dit l'état de la veille (état d'énergie + clé i18n)")
_e = core_pool.etat_energie(NA, force=True)
verifie(_e.get("veille_mesuree") is True and isinstance(_e.get("veille_coupee"), str),
        "12a. ★ `etat_energie` publie veille_mesuree + veille_coupee (sans interface cpuidle, "
        "`veille_mesuree` reste False : « rien à couper » n'est pas « non coupée »)", _e)
with open(os.path.join(RACINE, "static/js/cores.js"), encoding="utf-8") as f:
    _js = f.read()
verifie("settings.cores.energie_veille" in _js and "veille_mesuree" in _js,
        "12b. la fenêtre des cœurs l'affiche")
for _f in ("i18n/fr.json", "i18n/en.json"):
    with open(os.path.join(RACINE, _f), encoding="utf-8") as fh:
        verifie("settings.cores.energie_veille" in json.load(fh), "12c. clé i18n dans %s" % _f)

print("\n13. ★ Le BUDGET de réveil : réglage PAR NŒUD, posé ET mesuré avec la même valeur")
from app import settings as _settings                                           # noqa: E402
from app.database import db_set_node_setting                                    # noqa: E402

verifie(mtl.cpuidle_budget_us() == 5,
        "13a. défaut global à 5 µs (mesuré sur dl360-1 : entre C1 à 2 et C1E à 10)",
        mtl.cpuidle_budget_us())
# Un nœud n'a qu'un modèle de CPU : le réglage par nœud EST le réglage par modèle, sans table.
db_set_node_setting(NA, "cpuidle_latency_budget_us", 20)
verifie(mtl.cpuidle_budget_us(NA) == 20 and mtl.cpuidle_budget_us() == 5,
        "13b. ★★ l'override par nœud prime, et ne contamine pas le global",
        (mtl.cpuidle_budget_us(NA), mtl.cpuidle_budget_us()))
_hote_neuf(isolated="")
_hote["cmds"].clear()
acted, ok, msg = mtl.sync_dedicated_cpufreq({"id": NA}, [1, 2])
verifie(acted and ok and _veilles() == {1: {133}, 2: {133}},
        "13c. ★★ budget à 20 µs → seul C6 (133) est coupé, C1E (10) est GARDÉ : le budget est "
        "une durée d'échéance, pas une propriété du CPU", (acted, ok, msg, _veilles()))
_hote["cmds"].clear()
acted, ok, _m = mtl.sync_dedicated_cpufreq({"id": NA}, [1, 2])
verifie(not acted and ok and not _installs(),
        "13d. ★★ et la CONFORMITÉ se mesure avec le MÊME budget : sans ça le nœud se ferait "
        "réinstaller à chaque passage sans que rien puisse le satisfaire", _m)
# Échappatoire : un budget plus grand que tous les états annoncés n'en coupe plus aucun.
db_set_node_setting(NA, "cpuidle_latency_budget_us", 100000)
_hote_neuf(isolated="")
acted, ok, _m = mtl.sync_dedicated_cpufreq({"id": NA}, [1, 2])
verifie(acted and ok and _veilles() == {},
        "13e. ★ budget géant = coupure ÉTEINTE pour ce nœud (échappatoire par nœud, comme "
        "agent_token_inject=0), et le plancher de fréquence tient toujours",
        (acted, ok, _m, _veilles()))
verifie(_planchers() == {1, 2}, "13f. … le plancher de fréquence, lui, est bien resté", _etat())
db_set_node_setting(NA, "cpuidle_latency_budget_us", None)
verifie(mtl.cpuidle_budget_us(NA) == 5, "13g. override retiré → retour au global",
        mtl.cpuidle_budget_us(NA))
verifie("cpuidle_latency_budget_us" in _settings.DEFAULTS,
        "13h. le réglage est DÉCLARÉ (sinon il n'apparaît nulle part et ne se documente pas)")
with open(os.path.join(RACINE, "static/js/cores.js"), encoding="utf-8") as f:
    _jsc = f.read()
verifie("veille_budget_us" in _jsc and "veille_etats_us" in _jsc,
        "13i. ★ la fenêtre des cœurs montre le budget EN REGARD de l'échelle annoncée par ce CPU "
        "(un « 5 » nu ne dit pas ce qu'il coupe)")

print("\n" + ("─" * 70))
shutil.rmtree(_TMPDIR, ignore_errors=True)
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ cœurs dédiés en performance avec plancher max et veille profonde coupée, sur tout nœud ; "
      "plancher retiré et veille rendue à la libération ; mesure avant d'agir ; échec alerté")
