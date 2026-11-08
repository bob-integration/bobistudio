#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""I4 : un cpuset juste ne garantit rien si les FILS du conteneur le débordent.

★ LE CONSTAT (dell-1, 2026-11-08). Trois conteneurs d'outillage avaient
`Cpus_allowed_list 0-47` alors que leur cgroup valait `42-45` — et leurs fils tournaient sur les
cœurs 15 et 18, ceux du moteur. Le cpuset du conteneur était JUSTE : ce sont les processus lancés
par `docker exec` qui héritent des cœurs autorisés de leur LANCEUR (l'agent-nœud, donc toute la
machine), pas de ceux du conteneur. Aucun invariant ne regardait là : I1 juge le cpuset DEMANDÉ,
I2/I3 ne jugent que les conteneurs qui n'en ont AUCUN.

★ TROIS CHOSES QUE CETTE GARDE EXISTE POUR EMPÊCHER DE PERDRE.

1. **LA BONNE SOURCE.** `HostConfig.CpusetCpus` dit ce qui a été demandé À LA CRÉATION ;
   `cpuset.cpus.effective` du cgroup dit ce que le noyau APPLIQUE. Les deux diffèrent dès qu'un
   `docker update` est passé — et c'était le cas mesuré, HostConfig vide et cgroup à `42-45`. Se
   comparer au premier, c'est ne rien voir.
2. **LE SENS DE L'INCLUSION.** Un fil fixé plus ÉTROIT que son cgroup est normal et recherché :
   les lcores DPDK du moteur, le fil audio FIFO de la console. Un invariant symétrique crierait
   sur la configuration correcte — et « réparer » en élargissant un tel fil a cassé l'épinglage
   du moteur 619 le 2026-11-08, qu'il a fallu redémarrer.
3. **L'ABSTENTION DOIT SE VOIR.** Sans contrôleur `cpuset` sur le cgroup, il n'y a RIEN à
   comparer. Un invariant qui ne s'applique nulle part et ne le dit pas se confond avec un
   invariant toujours satisfait (`i4_non_mesure`).

Ce qu'elle tient :
1. ★★ fils autorisés hors du cgroup → défaut, avec le masque ET le cpuset du noyau ;
2. ★★ recoupement avec la bande isolée ou le cœur dédié d'un AUTRE → code et niveau distincts ;
3. ★★ fil fixé plus étroit → AUCUN défaut (le cas mtl_sch / fil audio) ;
4. ★ un `psr` vu hors du cgroup ne suffit pas : c'est le masque qui fait le défaut, le fil vu en
   est la preuve (et elle change la clé i18n) ;
5. ★★ cgroup illisible → aucun défaut, mais l'abstention est PUBLIÉE ;
6. ★ la sonde lit bien les deux champs, et elle s'exécute ;
7. les clés i18n des quatre combinaisons existent dans les DEUX catalogues, avec leurs paramètres.

Hors ligne : base jetable, aucun nœud joignable, relevés fabriqués.

MUTATIONS (I4_MUTATION=<nom>) — chacune doit donner rc=1 :
    i4_muet              le constat n'émet plus rien
    i4_cpuset_demande    il compare à `HostConfig.CpusetCpus` au lieu du cgroup
    i4_symetrique        l'inclusion devient symétrique (le fil étroit redevient un défaut)
    i4_un_seul_code      bande isolée / cœur dédié ne se distinguent plus du pool partagé
    i4_sans_preuve       les fils VUS ne sont plus rapportés
    i4_abstention_muette le non-mesurable n'est plus publié
    sonde_sans_masque    la sonde ne relève plus les masques d'affinité

    ./venv/bin/python tests/verif_fils_hors_cgroup.py
"""
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_i4_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool, placement                                    # noqa: E402

MUT = os.environ.get("I4_MUTATION") or ""
MUTATIONS = {
    "i4_muet": ("app/placement.py",
                '            if hors:\n'
                '                # Le masque est le DÉFAUT',
                '            if False:\n'
                '                # Le masque est le DÉFAUT'),
    # ⚠ Celle-ci REFAIT l'erreur de source : comparer au cpuset DEMANDÉ à la création.
    "i4_cpuset_demande": ("app/placement.py",
                          '        _cg = core_pool.parse_cpuset(c.get("cg_cpuset")) '
                          'if c.get("cg_cpuset") else set()',
                          '        _cg = core_pool.parse_cpuset(c.get("cpuset"))'),
    "i4_symetrique": ("app/placement.py",
                      '                for x in core_pool.parse_cpuset(_masque) - _cg:',
                      '                for x in core_pool.parse_cpuset(_masque) ^ _cg:'),
    "i4_un_seul_code": ("app/placement.py",
                        '                _code = "fils_hors_cpuset_reserve" if _grave '
                        'else "fils_hors_cpuset"',
                        '                _code = "fils_hors_cpuset"'),
    "i4_sans_preuve": ("app/placement.py",
                       '                if _vus:\n'
                       '                    _p4.update({"fmt_vus"',
                       '                if False:\n'
                       '                    _p4.update({"fmt_vus"'),
    "i4_abstention_muette": ("app/placement.py",
                             '            i4_muets.append(nom)',
                             '            pass'),
    "sonde_sans_masque": ("app/placement.py",
                          '                    if l2.startswith("Cpus_allowed_list:"):',
                          '                    if False:'),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _f, _old, _new = MUTATIONS[MUT]
    with open(os.path.join(RACINE, _f), encoding="utf-8") as _fh:
        _src = _fh.read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) dans %s — la garde est périmée"
              % (MUT, _src.count(_old), _f))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), placement.__file__, "exec"),   # noqa: S102
         placement.__dict__)
    print("MUTATION active :", MUT)

echecs = []
total = [0]


def verifie(ok, titre, detail=""):
    total[0] += 1
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Montage : un nœud avec moteur 2110 (bande isolée 15-21) et une console sur un cœur dédié ──
NID = database.db_add_node("dell-1", "192.0.2.20")
database.db_update_node(NID, compute_cpuset="22-23,42-45")
core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
placement.variante_mxl = lambda nid, force=False: None

CONSOLE = 1323
database.db_upsert_container_docker(CONSOLE, "console-1", NID, "bobi-cmp-%d" % CONSOLE)
database.db_update_deploy_config(CONSOLE, "audio_console", {})
core_pool.allocate_cores(NID, CONSOLE, 1)
_ded = core_pool.allocations_by_vmid(NID).get(CONSOLE) or []
assert _ded, "le montage n'a dédié aucun cœur : la garde ne mesurerait rien"
COEUR = _ded[0]
ISO = "15-21"
print("montage : bande isolée %s, console #%d sur le cœur dédié %d" % (ISO, CONSOLE, COEUR))


def constat(conteneurs, iso=ISO):
    return placement.constater(NID, releve={"isolated": iso, "nproc": 48,
                                            "containers": conteneurs})


def cont(nom, cpuset="", cg="42-45", allowed=None, threads=None):
    return {"name": nom, "cpuset": cpuset, "cpu_shares": 0,
            "threads": threads or {}, "cg_cpuset": cg,
            "allowed": allowed if allowed is not None else {}}


def defauts(res, code=None):
    return [d for d in res["defauts"] if code is None or d["code"] == code]


print("\n1. ★★ Des fils autorisés hors du cgroup, sans recoupement")
# Le cas MESURÉ, mais borné au pool partagé pour isoler CE défaut-là : HostConfig vide (le cpuset
# a été posé par `docker update`), cgroup à 42-43, fils autorisés sur tout le pool.
r = constat([cont("bobi-rust-dev", cpuset="", cg="42-43", allowed={"42-45": 4})])
d = defauts(r, "fils_hors_cpuset")
verifie(len(d) == 1, "1a. ★★ le défaut est émis", [x["code"] for x in r["defauts"]])
verifie(d and d[0]["niveau"] == "warning",
        "1b. … en `warning` : déborder sur du pool partagé n'est pas déborder sur le moteur",
        d and d[0]["niveau"])
_p = (d[0]["msg_params"] if d else {})
verifie(_p.get("fmt_cgroup") == "42-43",
        "1c. ★★ il nomme le cpuset que le NOYAU applique (pas celui demandé à la création)", _p)
verifie(core_pool.parse_cpuset(_p.get("fmt_hors") or "") == {44, 45},
        "1d. ★ et il nomme les cœurs EN TROP, pas le masque entier", _p.get("fmt_hors"))
verifie(d and d[0]["msg_key"] == "alert.resource.placement_fils_hors_cpuset",
        "1e. aucun fil vu dessus → la clé SANS preuve", d and d[0]["msg_key"])

print("\n2. ★★ Recoupement avec la bande isolée ou le cœur dédié d'un autre")
r = constat([cont("bmx-dev", cpuset="", cg="42-45", allowed={"0-47": 3},
                  threads={"15": 2, "18": 1, "43": 7})])
d = defauts(r, "fils_hors_cpuset_reserve")
verifie(len(d) == 1,
        "2a. ★★ code DISTINCT (`fils_hors_cpuset_reserve`) : ni la même conséquence ni le même "
        "remède, et la clé d'épisode est (nœud, conteneur, code)",
        [x["code"] for x in r["defauts"]])
verifie(d and d[0]["niveau"] == "error", "2b. ★ en `error`", d and d[0]["niveau"])
_g = core_pool.parse_cpuset((d[0]["msg_params"] if d else {}).get("fmt_grave") or "")
verifie({15, 18} <= _g and COEUR in _g,
        "2c. ★★ les cœurs du moteur ET le cœur dédié de la console sont nommés", sorted(_g))
verifie(d and d[0]["msg_key"].endswith("_vus")
        and (d[0]["msg_params"].get("n_vus") or 0) >= 3,
        "2d. ★ les fils VUS dessus sont la PREUVE, et ils changent la clé",
        d and (d[0]["msg_key"], d[0]["msg_params"].get("n_vus")))

print("\n3. ★★ Le sens de l'inclusion : un fil fixé PLUS ÉTROIT est normal")
r = constat([cont("bobi-mtl-900", cpuset="15-21,22", cg="15-21,22",
                  allowed={"15": 1, "16": 1, "22": 3}, threads={"15": 1, "16": 1, "22": 3})])
verifie(not defauts(r, "fils_hors_cpuset") and not defauts(r, "fils_hors_cpuset_reserve"),
        "3a. ★★ lcores DPDK épinglés un par un dans leur cgroup → AUCUN défaut (l'élargir a cassé "
        "l'épinglage du moteur 619 le 08/11)", [x["code"] for x in r["defauts"]])
r = constat([cont("console-1", cpuset="22-23", cg="22-23",
                  allowed={"22": 1, "22-23": 10}, threads={"22": 400, "23": 20})])
verifie(not defauts(r, "fils_hors_cpuset"),
        "3b. ★ le fil audio FIFO seul sur son cœur, les fils Python sur les deux → rien à dire",
        [x["code"] for x in r["defauts"]])
r = constat([cont("mixte", cpuset="42-45", cg="42-45",
                  allowed={"43": 2, "42-47": 1})])
d = defauts(r, "fils_hors_cpuset")
verifie(len(d) == 1 and d[0]["msg_params"]["n_threads"] == 1,
        "3c. ★★ un masque étroit ET un masque large → seul le large est compté",
        d and d[0]["msg_params"])

print("\n4. ★ Un `psr` hors du cgroup ne fait pas le défaut à lui seul")
r = constat([cont("vieux", cpuset="42-45", cg="42-45", allowed={"42-45": 6},
                  threads={"15": 3, "43": 2})])
verifie(not defauts(r, "fils_hors_cpuset") and not defauts(r, "fils_hors_cpuset_reserve"),
        "4a. ★ masque conforme → aucun défaut I4, même avec un `psr` ailleurs (le dernier cœur "
        "d'un fil endormi est un souvenir, pas une position)", [x["code"] for x in r["defauts"]])

print("\n5. ★★ Ce qui n'est pas mesurable est PUBLIÉ")
r = constat([cont("sans-cgroup", cpuset="42-45", cg=None, allowed={"0-47": 3}),
             cont("sans-masque", cpuset="42-45", cg="42-45", allowed={})])
verifie(not defauts(r, "fils_hors_cpuset") and not defauts(r, "fils_hors_cpuset_reserve"),
        "5a. ★ cgroup illisible (hiérarchie v1) ou masques absents → on ne conclut PAS",
        [x["code"] for x in r["defauts"]])
verifie(sorted(r.get("i4_non_mesure") or []) == ["sans-cgroup", "sans-masque"],
        "5b. ★★ … et l'abstention est nommée : un invariant muet se confond avec un invariant "
        "satisfait", r.get("i4_non_mesure"))
r = constat([cont("borne", cpuset="42-45", cg="42-45", allowed={"42-45": 3})])
verifie((r.get("i4_non_mesure") or []) == [],
        "5c. un conteneur mesuré n'est pas compté comme non mesurable", r.get("i4_non_mesure"))

print("\n6. ★ La sonde relève bien les deux champs, et elle s'exécute")
_src = placement._SONDE
verifie("Cpus_allowed_list" in _src,
        "6a. ★ elle lit le masque d'affinité de chaque fil (`/proc/<pid>/task/<tid>/status`)")
verifie("cpuset.cpus.effective" in _src and "/sys/fs/cgroup" in _src,
        "6b. ★★ et le cpuset EFFECTIF du cgroup — la seule source qui dise ce que le noyau applique")
_r = subprocess.run([sys.executable, "-"], input=_src, capture_output=True, text=True, timeout=120)
_j = None
try:
    _j = json.loads((_r.stdout or "").strip().splitlines()[-1])
except Exception:                                                       # noqa: BLE001
    pass
verifie(_r.returncode == 0 and isinstance(_j, dict) and "containers" in _j,
        "6c. ★ la sonde tourne et rend du JSON (elle est exécutée par `python3 -` sur le nœud : "
        "une faute de frappe y est invisible jusqu'au relevé suivant)",
        (_r.returncode, (_r.stderr or "")[:200]))

print("\n7. Les clés i18n des quatre combinaisons")
_cat = {}
for _l in ("fr", "en"):
    with open(os.path.join(RACINE, "i18n", "%s.json" % _l), encoding="utf-8") as _fh:
        _cat[_l] = json.load(_fh)
_attendus = {
    "alert.resource.placement_fils_hors_cpuset": ("nom", "n_threads", "fmt_hors", "fmt_cgroup"),
    "alert.resource.placement_fils_hors_cpuset_vus": ("n_vus", "fmt_vus"),
    "alert.resource.placement_fils_hors_cpuset_reserve": ("fmt_grave",),
    "alert.resource.placement_fils_hors_cpuset_reserve_vus": ("fmt_vus", "fmt_grave"),
}
for _cle, _params in _attendus.items():
    _ok = all(_cle in _cat[_l] for _l in ("fr", "en"))
    verifie(_ok, "7. %s existe dans les deux catalogues" % _cle)
    if not _ok:
        continue
    for _l in ("fr", "en"):
        _manque = [p for p in _params + ("n",) if ("{%s}" % p) not in _cat[_l][_cle]]
        verifie(not _manque, "7. %s [%s] porte tous ses paramètres" % (_cle, _l), _manque)

print("\n%d vérification(s), %d échec(s)" % (total[0], len(echecs)))
if echecs:
    print("ÉCHECS :")
    for e in echecs:
        print("  -", e)
sys.exit(1 if echecs else 0)
