#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent-nœud 0.32.4 : ce que le processeur offre pour protéger la MÉMOIRE d'un cœur dédié (2026-11-09).

Une partition cpuset protège le cœur, pas son cache ni sa bande passante. Mesuré sur dell-1 : une console son
isolée occupe ≈ 2 Mo de L3 ; sous saturation mémoire, son trafic vers la RAM passe de 32 à 83 Mo/s (son cache
est vidé par les autres) et un bloc calcule 600 µs au lieu de 70. Les leviers (Intel RDT) dépendent du
processeur : réservation de cache (CAT, Xeon E5 v4 et +), plafonnement de bande passante (MBA, Xeon Scalable
et +). L'agent les DÉTECTE, en lecture seule, et la fiche du nœud les affiche. Rien n'est réservé ici.

MUTATIONS (le test DOIT échouer) — QOS_MUT=<nom> :
    nomba       le plafonnement n'est jamais vu
    noinfo      ce que le noyau accorde (groupes, minimum) n'est pas lu quand resctrl est monté

    ./venv/bin/python tests/verif_agent_memory_qos.py
"""
import importlib.util
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
MUT = os.environ.get("QOS_MUT", "")
if MUT not in ("", "nomba", "noinfo"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


spec = importlib.util.spec_from_file_location("bobi_agent_qos", os.path.join(RACINE, "node_agent", "agent.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
TMP = tempfile.mkdtemp(prefix="verif_qos_")


def ecrire(path, txt):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(txt)


def machine(flags, cpus=4, l3=("56320K", "20"), mounted=False, resctrl_fs=True):
    """A fake host: cpuinfo flags, 2 L3 domains, optional resctrl info tree."""
    d = tempfile.mkdtemp(dir=TMP)
    ecrire(d + "/cpuinfo", "processor\t: 0\nflags\t\t: fpu sse2 %s\n\n" % " ".join(flags))
    ecrire(d + "/filesystems", "nodev\tsysfs\n" + ("nodev\tresctrl\n" if resctrl_fs else ""))
    for c in range(cpus):
        base = "%s/sys/cpu%d/cache/index3" % (d, c)
        ecrire(base + "/level", "3")
        ecrire(base + "/id", str(c % 2))
        ecrire(base + "/size", l3[0])
        ecrire(base + "/ways_of_associativity", l3[1])
    os.makedirs(d + "/sys/cpufreq", exist_ok=True)          # not a cpuN directory
    if mounted:
        for k, v in (("L3/num_closids", "16"), ("L3/min_cbm_bits", "1"), ("L3/shareable_bits", "c0000"),
                     ("MB/bandwidth_gran", "10"), ("MB/min_bandwidth", "10")):
            ecrire(d + "/resctrl/info/" + k, v)
    A.SYS_CPU, A.RESCTRL = d + "/sys", d + "/resctrl"
    if MUT == "noinfo":
        A.RESCTRL = d + "/absent"
    q = A._memory_qos(cpuinfo=d + "/cpuinfo", filesystems=d + "/filesystems")
    if MUT == "nomba":
        q["bw_limit"] = False
    return q


print("── 1. détection ────────────────────────────────────────────────────────────────")
q = machine(["rdt_a", "cat_l3", "cdp_l3", "cqm_llc", "cqm_mbm_total", "cqm_mbm_local"])
verifie(q["cache_alloc"] and not q["bw_limit"] and q["monitor"],
        "1a. ★ Xeon E5 v4 (dell-1) : réservation de cache, PAS de plafonnement, mesure", q)
verifie(q["l3_kb"] == 56320 and q["l3_ways"] == 20 and q["l3_domains"] == 2,
        "1b. taille, nombre de parts et domaines du L3 (un par processeur)", q)
q = machine(["rdt_a", "cat_l3", "mba", "cqm_llc", "cqm_mbm_total"], l3=("36608K", "11"), mounted=True)
verifie(q["cache_alloc"] and q["bw_limit"], "1c. ★ Xeon Scalable (dl360-1, Z8) : réservation ET plafonnement", q)
verifie(q.get("groups") == 16 and q.get("min_ways") == 1 and q.get("io_shared_mask") == "c0000"
        and q.get("bw_step_pct") == 10 and q.get("mounted") is True,
        "1d. resctrl monté : ce que le noyau accorde (groupes, minimum, parts partagées avec les E/S, pas du plafond)", q)
q = machine(["fpu"], resctrl_fs=False)
verifie(not q["cache_alloc"] and not q["bw_limit"] and not q["monitor"] and not q["kernel"],
        "1e. vieux processeur (r620) : rien, et c'est dit")
verifie("groups" not in machine(["cat_l3"]), "1f. resctrl non monté : rien n'est inventé")

print("── 2. branchements ─────────────────────────────────────────────────────────────")
SRC = open(os.path.join(RACINE, "node_agent", "agent.py"), encoding="utf-8").read()
m = SRC[SRC.index("def _machine():"):]
verifie('"memory_qos": _memory_qos()' in m[:m.index("_MACHINE.update")], "2a. l'inventaire de la machine porte memory_qos")
verifie("mount" not in SRC[SRC.index("def _memory_qos("):SRC.index("def _machine():")].replace('"mounted"', "").replace("doc[\"mounted\"]", ""),
        "2b. lecture seule : la détection ne monte rien")
NS = open(os.path.join(RACINE, "app", "routes", "node_sheet.py"), encoding="utf-8").read()
verifie('"memory_qos": machine.get("memory_qos")' in NS, "2c. la fiche du nœud le transmet")
JS = open(os.path.join(RACINE, "static", "node_sheet.js"), encoding="utf-8").read()
verifie("row(T('memqos'), memQosHtml(m))" in JS, "2d. et l'affiche (ligne « Gestion de la mémoire »)")
import json  # noqa: E402
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    verifie(all(cat.get("settings.node_sheet." + k) for k in ("memqos", "memqos_cat", "memqos_cat_short", "memqos_mba",
                                                             "memqos_no_mba", "memqos_none")),
            "2e. textes de la fiche (%s)" % lang)
verifie(tuple(int(x) for x in A.VERSION.split(".")) >= (0, 32, 4), "2f. version de l'agent ≥ 0.32.4", A.VERSION)

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    for e in echecs:
        print("  - " + e)
    sys.exit(1)
print("OK : l'agent dit ce que le processeur offre pour protéger la mémoire d'un cœur dédié")
