#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent-nœud 0.32.0 : les cœurs dédiés sont ISOLÉS par le noyau (partition cpuset, 2026-11-09).

Le constat (dell-1, console audio à 32/72, ftrace sur le CPU 13, 45 min) : avec l'agent et Docker bornés,
il restait 9 réveils > 300 µs (pire 1,09 ms) — des conteneurs jetables lancés sans --cpuset-cpus (scan
des médias, sonde de placement) et le worker udev réveillé par leur veth. Le même cœur en partition
`isolated`, ~170 conteneurs jetables provoqués exprès : 0 retard, pire réveil 227 µs.

Le noyau est simulé par une arborescence de fichiers ; une partition est refusée (« invalid ») tant
qu'un voisin MEMBRE liste un de ses CPU explicitement — c'est ce qui s'est passé sur dell-1.

MUTATIONS (le test DOIT échouer) — ISO_MUT=<nom> :
    nofree      les listes explicites des voisins ne sont pas allégées
    allroot     toujours `root`, même pour un seul CPU
    noslice     system.slice ne prête pas les CPU (cpuset.cpus.exclusive)
    norelease   une partition plus voulue n'est pas rendue
    engine      le moteur 2110 est isolé aussi
    samepush    le contrôleur pousse même quand l'agent a déjà la bonne carte
    noguard     les CPU réservés que personne n'utilise ne sont pas gardés (0.32.1)
    nosibling   le contrôleur n'ajoute pas le jumeau HT fermé par une réservation (0.32.1)
    noirq       les interruptions ne sont pas écartées des cœurs isolés (0.32.2)
    widen       l'affectation d'une IRQ est réécrite en « tout sauf » au lieu d'être rétrécie
    nogiveback  ce qui a été retiré n'est pas rendu à la libération
    nomce       le sondage MCE n'est jamais coupé (0.32.3)
    mcealways   le sondage MCE est coupé même sans cœur isolé
    mceold      le contrôleur demande `mce_off` à un agent qui ne le connaît pas

    ./venv/bin/python tests/verif_agent_isolation.py
"""
import importlib.util
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
MUT = os.environ.get("ISO_MUT", "")
if MUT not in ("", "nofree", "allroot", "noslice", "norelease", "engine", "samepush", "noguard", "nosibling",
               "noirq", "widen", "nogiveback", "nomce", "mcealways", "mceold"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


def charger_agent():
    chemin = os.path.join(RACINE, "node_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("bobi_node_agent_iso_sous_test", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = charger_agent()
SRC = open(os.path.join(RACINE, "node_agent", "agent.py"), encoding="utf-8").read()
TMP = tempfile.mkdtemp(prefix="verif_iso_")
A.CG_ROOT = os.path.join(TMP, "cg")
A.ISO_PATH = os.path.join(TMP, "isolation.json")
A.PROC_IRQ = os.path.join(TMP, "irq")          # NEVER the host's /proc/irq
os.makedirs(A.PROC_IRQ)
A.MCE_DIR = os.path.join(TMP, "machinecheck")  # NEVER the host's machine-check polling
A.MCE_SAVED = os.path.join(TMP, "mce_check_interval.saved")
os.makedirs(A.MCE_DIR + "/machinecheck0")
with open(A.MCE_DIR + "/machinecheck0/check_interval", "w") as _fh:
    _fh.write("300")
SL = A.CG_ROOT + "/system.slice"
CGS = {}                                       # container name -> cgroup rel path
ECRITURES = []


def cg(rel, cpus="", partition="member", exclusive=None):
    d = A.CG_ROOT + rel
    os.makedirs(d, exist_ok=True)
    for f, v in (("cpuset.cpus", cpus), ("cpuset.cpus.partition", partition)):
        with open(d + "/" + f, "w") as fh:
            fh.write(v)
    if exclusive is not None:
        with open(d + "/cpuset.cpus.exclusive", "w") as fh:
            fh.write(exclusive)


def lire(rel, f):
    with open(A.CG_ROOT + rel + "/" + f) as fh:
        return fh.read().strip()


_vrai_write = A._cg_write


def noyau_write(path, value):
    """The kernel, in miniature: a partition stays `invalid` while a MEMBER sibling (at the
    system.slice level or the root level) lists one of its CPUs explicitly."""
    ECRITURES.append((path[len(A.CG_ROOT):], value))
    if path.endswith("/smp_affinity_list") and os.path.exists(os.path.join(os.path.dirname(path), "managed")):
        return "[Errno 5] Input/output error"   # kernel-managed IRQ: affinity is fixed
    err = _vrai_write(path, value)
    if err or not path.endswith("/cpuset.cpus.partition") or value not in ("isolated", "root"):
        return err
    d = os.path.dirname(path)
    mine = A._parse_cpulist(open(d + "/cpuset.cpus.exclusive").read().strip() or "")
    for parent, skip in ((os.path.dirname(d), d), (A.CG_ROOT, os.path.dirname(d))):
        for child in os.listdir(parent):
            s = os.path.join(parent, child)
            if s == skip or not os.path.isdir(s):
                continue
            if not (A._cg_read(s + "/cpuset.cpus.partition") or "member").startswith("member"):
                continue
            raw = A._cg_read(s + "/cpuset.cpus") or ""
            if raw and A._parse_cpulist(raw) & mine:
                _vrai_write(path, value + " invalid (Cpu list in cpuset.cpus not exclusive)")
                return None
    return None


A._cg_write = noyau_write
A._cg_rmdir = lambda p: __import__("shutil").rmtree(p)   # a real cgroup dir's files are virtual
A._cg_of_pid = lambda pid: CGS.get({1001: "bobi-cmp-1339", 1002: "bobi-cmp-1250", 1003: "bobi-cmp-7",
                                     1004: "bobi-cmp-8"}.get(pid))
PIDS = {"bobi-cmp-1339": 1001, "bobi-cmp-1250": 1002, "bobi-cmp-7": 1003, "bobi-cmp-8": 1004}
fake_pids = lambda names: {n: PIDS[n] for n in names if n in PIDS}

if MUT == "nofree":
    A._iso_free_siblings = lambda *a, **k: None
if MUT == "allroot":
    _w0 = A._cg_write
    A._cg_write = lambda p, v: _w0(p, "root" if v == "isolated" else v)
if MUT == "noguard":
    _wg = A._cg_write
    A._cg_write = lambda p, v: None if "/bobi-reserved/" in p else _wg(p, v)
if MUT == "noslice":
    _w = A._cg_write
    A._cg_write = lambda p, v: None if p == SL + "/cpuset.cpus.exclusive" else _w(p, v)

# The dell-1 tree of 2026-11-09, in small: root-level user.slice and init.scope at 0-87, services at
# 0-87, a stale RDMA target listing 13, a sibling pinned on 13 alone, the console, a 3-core mixer.
cg("/user.slice", "0-87")
cg("/init.scope", "0-87")
cg("/system.slice", "", exclusive="")
cg("/system.slice/ssh.service", "0-87")
cg("/system.slice/docker.service", "0,15,17")
cg("/system.slice/docker-aaa.scope", "13,15,17")          # rdma-tgt-4092
cg("/system.slice/docker-con.scope", "13", exclusive="")  # bobi-cmp-1339 (audio console)
cg("/system.slice/docker-mix.scope", "20,22,24", exclusive="")
cg("/system.slice/docker-bad.scope", "30-31", exclusive="")
CGS.update({"bobi-cmp-1339": "/system.slice/docker-con.scope", "bobi-cmp-1250": "/system.slice/docker-mix.scope",
            "bobi-cmp-7": "/system.slice/docker-bad.scope"})

# ================================================================ 1. l'agent pose les partitions
print("── 1. partitions ───────────────────────────────────────────────────────────────")
A._ISO["wanted"] = {"bobi-cmp-1339": "13,57", "bobi-cmp-1250": "20,22,24", "bobi-cmp-7": "30",
                    "bobi-cmp-8": "40"}
r = A._iso_reconcile(_pids=fake_pids)
st = r["state"]
verifie(st.get("bobi-cmp-1339", {}).get("status") == "isolated" and lire("/system.slice/docker-con.scope",
        "cpuset.cpus.partition") == "isolated",
        "1a. ★ console (un CPU, réserve 13 + jumeau 57) : partition `isolated` sur SON cpuset 13", st.get("bobi-cmp-1339"))
verifie(st.get("bobi-cmp-1250", {}).get("status") == "root",
        "1b. mélangeur (3 CPU) : `root` — l'ordonnanceur répartit encore DANS la partition", st.get("bobi-cmp-1250"))
verifie(lire("/system.slice", "cpuset.cpus.exclusive") == "13,20,22,24,57",
        "1c. system.slice prête exactement l'union (jumeau gardé compris)", lire("/system.slice", "cpuset.cpus.exclusive"))
verifie(lire("/system.slice/ssh.service", "cpuset.cpus") == "0-12,14-19,21,23,25-56,58-87"
        and lire("/user.slice", "cpuset.cpus") == "0-12,14-19,21,23,25-56,58-87"
        and lire("/system.slice/docker-aaa.scope", "cpuset.cpus") == "15,17",
        "1d. ★ voisins qui listaient ces CPU (0-87, RDMA périmé) : allégés, aux deux niveaux",
        (lire("/system.slice/ssh.service", "cpuset.cpus"), lire("/user.slice", "cpuset.cpus")))
verifie(lire("/system.slice/docker.service", "cpuset.cpus") == "0,15,17",
        "1e. un voisin qui ne les liste pas n'est pas réécrit")
verifie(st.get("bobi-cmp-7", {}).get("status") == "cpuset_mismatch"
        and lire("/system.slice/docker-bad.scope", "cpuset.cpus.partition") == "member",
        "1f. cpuset HORS de la réservation (30-31 pour 30) : pas de partition, dit", st.get("bobi-cmp-7"))
verifie(st.get("bobi-cmp-8", {}).get("status") == "not_running", "1g. conteneur absent : `not_running`")
G = "/system.slice/bobi-reserved"
verifie([A._cg_read(A.CG_ROOT + G + "/" + f) for f in ("cpuset.cpus", "cpuset.cpus.exclusive", "cpuset.cpus.partition")]
        == ["57", "57", "isolated"],
        "1g bis. ★ jumeau réservé que la console n'utilise pas (57) : gardé dans une partition VIDE",
        os.listdir(A.CG_ROOT + "/system.slice"))

ECRITURES.clear()
A._iso_reconcile(_pids=fake_pids)
verifie(ECRITURES == [], "1h. ★ second passage sans changement : AUCUNE écriture (un nœud stable ne coûte rien)",
        ECRITURES[:4])

# A sibling that would be left with no CPU is a blocker: never emptied, reported.
cg("/system.slice/docker-pin.scope", "13")
A._cg_write(A.CG_ROOT + "/system.slice/docker-con.scope/cpuset.cpus.partition", "member")
r = A._iso_reconcile(_pids=fake_pids)
c = r["state"].get("bobi-cmp-1339", {})
verifie(c.get("status") == "invalid" and lire("/system.slice/docker-pin.scope", "cpuset.cpus") == "13"
        and any("docker-pin.scope" in b for b in c.get("blockers") or []),
        "1i. ★ voisin épinglé sur 13 seul : jamais vidé ; partition `invalid`, voisin NOMMÉ", c)
os.rename(A.CG_ROOT + "/system.slice/docker-pin.scope", TMP + "/pin-gone")

# ================================================================ 2. libération
print("── 2. libération ───────────────────────────────────────────────────────────────")
if MUT == "norelease":
    _w2 = A._cg_write
    A._cg_write = lambda p, v: None if (v in ("member", "") and "/docker-" in p) else _w2(p, v)
A._ISO["wanted"] = {"bobi-cmp-1339": "13"}
A._iso_reconcile(_pids=fake_pids)
verifie(lire("/system.slice/docker-mix.scope", "cpuset.cpus.partition") == "member"
        and lire("/system.slice/docker-mix.scope", "cpuset.cpus.exclusive") == ""
        and lire("/system.slice", "cpuset.cpus.exclusive") == "13",
        "2a. ★ mélangeur plus voulu : partition rendue, system.slice ne prête plus que 13",
        (lire("/system.slice/docker-mix.scope", "cpuset.cpus.partition"), lire("/system.slice", "cpuset.cpus.exclusive")))
verifie(not os.path.isdir(A.CG_ROOT + G), "2a bis. plus de jumeau réservé : la partition de garde disparaît")
A._ISO["wanted"] = {}
A._iso_reconcile(_pids=fake_pids)
verifie(lire("/system.slice/docker-con.scope", "cpuset.cpus.partition") == "member"
        and lire("/system.slice", "cpuset.cpus.exclusive") == "",
        "2b. carte vide : tout est rendu (le réglage `dedicated_cpu_isolation` = 0 y mène)")

# kernfs never sees a zero-byte write: clearing a list must still send bytes (dell-1, 2026-11-09 — the
# slice kept lending CPU 13 after every partition had been released).
_f = os.path.join(TMP, "kernfs")
with open(_f, "w") as fh:
    fh.write("13")
_vrai_write(_f, "")
verifie(open(_f, "rb").read() != b"", "2c. ★ vider une liste écrit au moins un octet (sinon le noyau ne voit rien)",
        open(_f, "rb").read())

# ================================================================ 3. validation, persistance, noyau trop vieux
print("── 3. entrée, disque, noyau ────────────────────────────────────────────────────")
verifie(A._iso_set({"bad name;rm": "13"}) is not None and A._iso_set({"x": "a-b"}) is not None
        and A._iso_set(["x"]) is not None, "3a. nom ou liste de CPU mal formés : refusés")
A._wd_pids = fake_pids
verifie(A._iso_set({"bobi-cmp-1339": "13"}) is None
        and json.load(open(A.ISO_PATH)) == {"containers": {"bobi-cmp-1339": "13"}, "mce_off": False},
        "3b. carte posée puis gardée sur disque")
A._ISO["wanted"] = {}
A._iso_load()
verifie(A._ISO["wanted"] == {"bobi-cmp-1339": "13"}, "3c. relue au démarrage")
os.remove(SL + "/cpuset.cpus.exclusive")
ECRITURES.clear()
r = A._iso_reconcile(_pids=fake_pids)
verifie(ECRITURES == [] and "unsupported" in (r.get("error") or ""),
        "3d. noyau sans partitions distantes : rien n'est écrit, et c'est DIT", r.get("error"))

# ================================================================ 4. contrôleur
print("── 4. le contrôleur pousse la carte ────────────────────────────────────────────")
from app import core_pool, node_driver, node_health  # noqa: E402
import app.database as _db  # noqa: E402

core_pool.allocations_by_vmid = lambda nid: {619: [0, 1, 2, 3], 1339: [13], 1250: [20, 22, 24], 999: [5]}
# Topology: CPU c and c+44 share a physical core. The pool closes 57 (sibling of the console's 13); the
# mixer's siblings 64,66,68 stay open to the shared pool.
core_pool.core_map_cached = lambda nid: {c: c % 44 for c in range(88)}
_db.db_get_node = lambda nid: {"id": nid, "compute_cpuset": "13,20,22,24,57,64,66,68"}
core_pool.partage_disponible = lambda nid, pool=None, core_of=None, **k: (
    [13, 20, 22, 24, 57, 64, 66, 68] if MUT == "nosibling" else [64, 66, 68], set())
_db.db_get_containers = lambda: [
    {"vmid": 619, "node_id": 35, "docker_name": "bobi-mtl-619", "deploy_config": json.dumps({"type": "2110_io"})},
    {"vmid": 1339, "node_id": 35, "docker_name": "bobi-cmp-1339", "deploy_config": json.dumps({"type": "audio_console"})},
    {"vmid": 1250, "node_id": 35, "docker_name": None, "deploy_config": "{}"},
    {"vmid": 1400, "node_id": 35, "docker_name": "bobi-cmp-1400", "deploy_config": "{}"},
    {"vmid": 1500, "node_id": 34, "docker_name": "bobi-cmp-1500", "deploy_config": "{}"},
]
reglages = {}
node_health.S.get = lambda k, d=None: reglages.get(k, d)
if MUT == "engine":
    _db.db_get_containers = (lambda f: (lambda: [dict(c, deploy_config="{}") for c in f()]))(_db.db_get_containers)
w = node_health.isolation_wanted(35)
verifie(w == {"bobi-cmp-1339": "13,57", "bobi-cmp-1250": "20,22,24"},
        "4a. ★ vivants avec réservation, moteur 2110 EXCLU, jumeau FERMÉ ajouté (57), jumeaux ouverts non", w)
reglages["dedicated_cpu_isolation"] = "0"
verifie(node_health.isolation_wanted(35) == {}, "4b. réglage à 0 : carte vide (tout est rendu)")
reglages.clear()

poussees, alertes = [], []
node_driver.set_isolation = lambda node, c, mce_off=None: (poussees.append(c) or True,
                                                           {"ok": True, "wanted": c, "state": {}, "error": None})
node_health.db_add_alert = lambda *a, **k: alertes.append((a, k))
if MUT == "samepush":
    _vrai_push = node_health._push_isolation

    def _toujours(node, snap):
        snap.setdefault("isolation", {})["wanted"] = {"force": "1"}
        return _vrai_push(node, snap)
    node_health._push_isolation = _toujours
n35 = {"id": 35, "agent_url": "https://dell-1:9100"}
snap = {"name": "dell-1", "isolation": {"wanted": {}, "state": {}}}
node_health._push_isolation(n35, snap)
verifie(poussees == [w], "4c. carte différente : poussée", poussees)
poussees.clear()
node_health._push_isolation(n35, {"name": "dell-1", "isolation": {"wanted": dict(w), "state": {}}})
verifie(poussees == [], "4d. déjà la bonne : rien n'est envoyé", poussees)
node_health._push_isolation(n35, {"name": "dell-1", "agent_version": "0.31.1"})
verifie(poussees == [], "4e. vieil agent (pas d'`isolation` dans sa santé) : laissé tranquille")

inv = {"status": "invalid", "cpus": "13", "partition": "isolated invalid (Cpu list in cpuset.cpus not exclusive)",
       "blockers": ["/system.slice/docker-pin.scope (13): would have no CPU left"]}
s_inv = lambda: {"name": "dell-1", "isolation": {"wanted": dict(w), "state": {"bobi-cmp-1339": dict(inv)}}}
node_health._push_isolation(n35, s_inv())
node_health._push_isolation(n35, s_inv())
verifie(len(alertes) == 1 and alertes[0][0][0] == "alert.resource.isolation_refused"
        and "docker-pin.scope" in alertes[0][1]["params"]["blockers"],
        "4f. ★ partition refusée : UNE alerte, qui nomme le voisin bloquant", alertes)
node_health._push_isolation(n35, {"name": "dell-1", "isolation": {"wanted": dict(w),
                                  "state": {"bobi-cmp-1339": {"status": "isolated"}}}})
node_health._push_isolation(n35, s_inv())
verifie(len(alertes) == 2, "4g. rétablie puis refusée à nouveau : réalertée", len(alertes))
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    t = cat.get("alert.resource.isolation_refused") or ""
    verifie(all("{%s}" % k in t for k in ("n", "name", "cpus", "reason", "blockers")),
            "4h. libellé de l'alerte (%s) avec tous ses paramètres" % lang)

# ================================================================ 5. branchements
print("── 5. branchements ─────────────────────────────────────────────────────────────")
NH = open(os.path.join(RACINE, "app", "node_health.py"), encoding="utf-8").read()
corps = NH[NH.index("def _sample_one"):NH.index("def sample_all")]
verifie("_push_isolation(node, snap)" in corps, "5a. le relevé de santé de chaque nœud pousse la carte")
m = SRC[SRC.index("def main():"):]
verifie("_iso_load()" in m and "target=_iso_loop" in m and m.index("_iso_load()") < m.index("target=_iso_loop"),
        "5b. au démarrage : carte relue puis fil de rattrapage (un conteneur recréé change de cgroup)")
verifie('"isolation": dict(_ISO)' in SRC and 'if path == "/v1/host/isolation":' in SRC,
        "5c. /v1/health remonte l'état ; route POST /v1/host/isolation")
verifie(tuple(int(x) for x in A.VERSION.split(".")) >= (0, 32, 3), "5d. version de l'agent ≥ 0.32.3", A.VERSION)
verifie("irq_moved, irq_unmovable = _iso_irqs(lent)" in SRC, "5e. la réconciliation écarte aussi les interruptions")

# ================================================================ 6. interruptions (0.32.2)
print("── 6. interruptions ────────────────────────────────────────────────────────────")
ONLINE, LENT = set(range(48)), {13, 14, 26}


def irq(n, aff, name, managed=False):
    d = os.path.join(A.PROC_IRQ, str(n))
    os.makedirs(os.path.join(d, name), exist_ok=True)
    with open(d + "/smp_affinity_list", "w") as fh:
        fh.write(aff)
    if managed:
        open(d + "/managed", "w").close()


def aff(n):
    with open(os.path.join(A.PROC_IRQ, str(n), "smp_affinity_list")) as fh:
        return fh.read().strip()


with open(A.PROC_IRQ + "/default_smp_affinity", "w") as fh:
    fh.write("ffff,ffffffff")
irq(279, "0-15", "eno3-rx-4")
irq(208, "13", "mlx4-65")
irq(101, "26", "megasas0-msix58", managed=True)
irq(142, "0,2,4", "ahci")
irq(300, "0,15-20", "bobi-band")                # 2110 node: 1-14 kept off by bobi-irq at boot
A._IRQ_TAKEN.clear()
if MUT == "noirq":
    A._iso_irqs = lambda lent, **k: ([], [])
if MUT == "widen":
    _p0 = A._irq_put
    A._irq_put = lambda key, cpus, raw: _p0(key, cpus if key == "default" or 13 in cpus else ONLINE - LENT, raw)
if MUT == "nogiveback":
    class _Oubli(dict):
        def setdefault(self, k, d=None):
            return [set(), set()]
    A._IRQ_TAKEN = _Oubli()
del ECRITURES[:]
moved, unmov = A._iso_irqs(LENT, _online=ONLINE, _isolated={30})
verifie(aff(279) == "0-12,15", "6a. ★ IRQ étalée : les CPU isolés sont retirés, le reste ne bouge pas", aff(279))
verifie(aff(208) == "0-12,15-25,27-29,31-47", "6b. IRQ posée sur un seul CPU isolé : repli hors isolés et hors bande", aff(208))
verifie(aff(101) == "26" and any(u.startswith("101 megasas0-msix58 on 26") for u in unmov),
        "6c. IRQ gérée par le noyau : laissée, et signalée par son nom", unmov)
verifie(aff(142) == "0,2,4" and not any("/142/" in p for p, _v in ECRITURES),
        "6d. IRQ hors des CPU isolés : aucune écriture", ECRITURES)
verifie(aff(300) == "0,15-20", "6e. ★ bande d'un nœud 2110 : jamais élargie", aff(300))
with open(A.PROC_IRQ + "/default_smp_affinity") as fh:
    dflt = fh.read().strip()
verifie(dflt == "0000ffff,fbff9fff", "6f. affectation par défaut des futures IRQ rétrécie, même format", dflt)
A._iso_irqs(set(), _online=ONLINE, _isolated={30})
with open(A.PROC_IRQ + "/default_smp_affinity") as fh:
    dflt = fh.read().strip()
verifie(aff(279) == "0-15" and aff(208) == "13" and dflt == "0000ffff,ffffffff",
        "6g. ★ partition rendue : chaque IRQ retrouve son affectation d'origine", (aff(279), aff(208), dflt))
A._iso_irqs(set(), _online=ONLINE, _isolated={30})
verifie(not A._IRQ_TAKEN, "6h. plus rien à rendre ensuite", dict(A._IRQ_TAKEN))

# ================================================================ 7. sondage MCE (0.32.3)
print("── 7. sondage MCE ──────────────────────────────────────────────────────────────")
MCE = A.MCE_DIR + "/machinecheck0/check_interval"


def mce():
    with open(MCE) as fh:
        return fh.read().strip()


def mce_set(v):
    with open(MCE, "w") as fh:
        fh.write(v)


if MUT == "nomce":
    A._iso_mce = lambda lent: {"status": "on"}
if MUT == "mcealways":
    _m0 = A._iso_mce
    A._iso_mce = lambda lent: _m0(lent or {1})
A._ISO["mce_off"] = False
A._iso_mce({13})
verifie(mce() == "300", "7a. réglage par défaut : le sondage reste (300 s)", mce())
A._ISO["mce_off"] = True
st = A._iso_mce({13})
verifie(mce() == "0" and st.get("status") == "off" and A._cg_read(A.MCE_SAVED) == "300",
        "7b. ★ demandé ET cœur isolé : coupé, valeur d'origine gardée sur disque", (mce(), st))
A._iso_mce(set())
verifie(mce() == "300" and not os.path.exists(A.MCE_SAVED),
        "7c. ★ plus aucun cœur isolé : rétabli à sa valeur d'origine", mce())
A._iso_mce({13})
A._ISO["mce_off"] = False                      # agent restarted with the choice turned back
st = A._iso_mce({13})
verifie(mce() == "300", "7d. retour au choix recommandé : rétabli (même après redémarrage, la valeur est sur disque)", (mce(), st))
mce_set("0")
A._ISO["mce_off"] = False
st = A._iso_mce({13})
verifie(mce() == "0" and st.get("status") == "off_by_host",
        "7e. coupé par l'hôte lui-même : laissé tel quel, et dit", st)
mce_set("300")
A._wd_pids = fake_pids
verifie(A._iso_set({"bobi-cmp-1339": "13"}, mce_off="oui") is not None, "7f. mce_off non booléen : refusé")
A._iso_set({"bobi-cmp-1339": "13"}, mce_off=True)
A._ISO["mce_off"] = False
A._iso_load()
verifie(A._ISO["mce_off"] is True and json.load(open(A.ISO_PATH)).get("mce_off") is True,
        "7g. le choix est gardé sur disque et relu au démarrage")
A._iso_set({}, mce_off=False)
recus = []
node_driver.set_isolation = lambda node, c, mce_off=None: (recus.append(mce_off) or True,
                                                           {"ok": True, "wanted": c, "state": {}, "error": None,
                                                            "mce_off": bool(mce_off)})
reglages.clear()
reglages["mce_polling"] = "off_when_isolated"
wv = node_health.isolation_wanted(35)
s_new = {"name": "dell-1", "isolation": {"wanted": dict(wv), "state": {}, "mce_off": False}}
node_health._push_isolation(n35, s_new)
verifie(recus == [True], "7h. ★ réglage « couper » : demandé à l'agent même si la carte n'a pas changé", recus)
recus.clear()
s_old = {"name": "dell-1", "isolation": {"wanted": dict(wv), "state": {}}}
if MUT == "mceold":
    s_old["isolation"]["mce_off"] = False
node_health._push_isolation(n35, s_old)
verifie(recus == [], "7i. agent < 0.32.3 (pas de `mce_off`) : rien ne lui est demandé", recus)
reglages.clear()
node_health._push_isolation(n35, {"name": "dell-1", "isolation": {"wanted": dict(wv), "state": {}, "mce_off": False}})
verifie(recus == [], "7j. défaut « garder » et agent d'accord : aucune poussée", recus)
SET = open(os.path.join(RACINE, "app", "settings.py"), encoding="utf-8").read()
HTML = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
AIDE = open(os.path.join(RACINE, "templates", "aide.html"), encoding="utf-8").read()
verifie('"mce_polling":                "keep",' in SET and 'id="s_mce_polling"' in HTML
        and "'mce_polling'" in HTML and 'value="off_when_isolated"' in HTML,
        "7k. réglage déclaré (défaut « garder ») et présent dans Réglages → CPU")
verifie("Réglages → CPU → Cœurs dédiés\u00a0: surveillance matérielle" in AIDE and "340 µs" in AIDE,
        "7l. l'aide explique le réglage et dit où il se trouve")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    verifie(all(cat.get("settings.cpu_iso." + k) for k in ("title", "intro", "mce_lbl", "mce_keep", "mce_off", "mce_hint")),
            "7m. textes du réglage (%s)" % lang)

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    for e in echecs:
        print("  - " + e)
    sys.exit(1)
print("OK : les cœurs dédiés deviennent des partitions du noyau, et un refus est nommé")
