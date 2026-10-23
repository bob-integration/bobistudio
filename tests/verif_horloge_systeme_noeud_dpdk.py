#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Horloge SYSTÈME d'un nœud 2110 full-PF DPDK : quand la rendre à chrony, et quand c'est interdit.

Contexte (dl360-1, 2026-10-16) : le phc2sys interne de libmtl (`ENGINE_PHC2SYS=1`) n'a pas d'option
d'offset — REALTIME portait du TAI, 37 s faux dans tous les journaux — puis son servo a laissé le
`tick` noyau à 10122 (+1,22 %) et échouait à chaque correction : 396 s d'avance sur l'UTC, et
toutes les répliques RDMA issues du nœud à zéro grain.

Ce que la garde vérifie, hors ligne et sans base :
  1. `docker_driver` pose ENGINE_PHC2SYS tant que libmtl n'asservit la fréquence du PHC qu'avec
     phc2sys actif (sinon : verrou PTP strict perdu, essayé et mesuré le 2026-10-17) ;
  2. `clocks._horloge_systeme_libre` ne rend la main à chrony QUE si le moteur lit le PHC sans
     discipliner REALTIME — jamais sur un doute ni quand un servo la tient ;
  3. `clocks.appliquer_ntp_tai` installe chrony sur un tel nœud même si REALTIME porte encore le TAI
     laissé par libmtl, le REFUSE si libmtl discipline encore, pose les sources du site et remet
     le `tick` au nominal AVANT de démarrer chrony ;
  4. la commande Python de remise au nominal compile (elle part entre apostrophes shell)."""
import ast, os, re, sys
RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import clocks, database, settings

ok_total = True


def cas(nom, attendu, obtenu):
    global ok_total
    ok = attendu == obtenu
    ok_total &= ok
    print("%s %-62s attendu=%-6s obtenu=%s" % ("OK  " if ok else "✕ KO", nom, attendu, obtenu))


# ── 1) docker_driver ne pose plus ENGINE_PHC2SYS ────────────────────────────────────────────────
src = open(os.path.join(RACINE, "app", "docker_driver.py")).read()
poses = []
for n in ast.walk(ast.parse(src)):
    # env["ENGINE_PHC2SYS"] = …
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "ENGINE_PHC2SYS"):
                poses.append(n.lineno)
    # env.setdefault("ENGINE_PHC2SYS", …) / env.update(ENGINE_PHC2SYS=…)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
        if (n.func.attr == "setdefault" and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "ENGINE_PHC2SYS"):
            poses.append(n.lineno)
        if n.func.attr == "update" and any(k.arg == "ENGINE_PHC2SYS" for k in n.keywords):
            poses.append(n.lineno)
# Le servo de FRÉQUENCE du PHC est, dans libmtl, sous `if (ptp->phc2sys_active)` : retirer
# ENGINE_PHC2SYS sans patch qui l'en sorte coûte le verrou PTP strict (mesuré 2026-10-17, dl360-1 :
# 0,5–2,7 µs au lieu de ~30 ns). Tant qu'aucun patch du moteur ne porte le marqueur ci-dessous,
# docker_driver DOIT poser ENGINE_PHC2SYS.
MARQUEUR_DECOUPLAGE = "bobi.studio: servo freq PHC hors phc2sys"
dock = os.path.join(RACINE, "plugins", "2110_io", "docker")
decouple = os.path.isdir(dock) and any(
    MARQUEUR_DECOUPLAGE in open(os.path.join(dock, f)).read()
    for f in os.listdir(dock) if f.startswith("patch_") and f.endswith(".py"))
if not os.path.isdir(dock):
    print("    (sous-module 2110_io absent : on suppose libmtl NON patché)")
if not decouple:
    cas("libmtl non découplé → docker_driver pose ENGINE_PHC2SYS", True, len(poses) >= 1)
else:
    cas("libmtl découplé → ENGINE_PHC2SYS n'est plus nécessaire (info)", True, True)
# Tant que ENGINE_PHC2SYS est posé, c'est le servo phc2sys de libmtl qui tient CLOCK_REALTIME — et
# le servo amont s'emballe sans retour dès qu'une excursion du PHC lui fait demander plus de ±10 %
# (dl360-1 : +396 s le 2026-10-16, −455 s le 2026-10-17). Le patch qui le borne est donc OBLIGATOIRE
# dans ce régime. Le marqueur de DÉCOUPLAGE ci-dessus reste distinct : borner le servo ne sort pas
# le servo de fréquence du PHC du `if (phc2sys_active)` — ENGINE_PHC2SYS reste nécessaire.
MARQUEUR_BORNE = "bobi.studio: phc2sys servo borne"
borne = os.path.isdir(dock) and any(
    MARQUEUR_BORNE in open(os.path.join(dock, f)).read()
    for f in os.listdir(dock) if f.startswith("patch_") and f.endswith(".py"))
if os.path.isdir(dock) and len(poses) >= 1:
    cas("ENGINE_PHC2SYS posé → le servo phc2sys est borné (patch présent)", True, borne)
    cas("le patch de bornage n'usurpe PAS le marqueur de découplage", False,
        MARQUEUR_DECOUPLAGE in open(os.path.join(dock, "patch_phc2sys_servo_bound.py")).read()
        if os.path.exists(os.path.join(dock, "patch_phc2sys_servo_bound.py")) else False)

# ── 2) _horloge_systeme_libre ───────────────────────────────────────────────────────────────────
NODE = {"id": 34, "name": "dl360-1"}


def avec_discipline(d):
    clocks._discipline = lambda node: d
    return clocks._horloge_systeme_libre(NODE)


cas("libmtl PTP sans PHC2SYS → libre",
    True, avec_discipline({"par": "moteur 2110 (média) + chrony", "phc2sys": False}))
cas("libmtl PTP + PHC2SYS → tenue",
    False, avec_discipline({"par": "moteur 2110", "phc2sys": True}))
cas("env illisible (phc2sys None) → pas libre",
    False, avec_discipline({"par": None, "phc2sys": None}))
cas("moteur sans PTP interne (AF-XDP, phc2sys noyau) → pas libre",
    False, avec_discipline({"par": "aucun", "phc2sys": False}))

# ── 3) appliquer_ntp_tai ────────────────────────────────────────────────────────────────────────
envoyes = []
database.db_get_node = lambda nid: dict(NODE)
settings.get = lambda k, d=None: "10.0.0.1, 10.0.0.2" if k == "ntp_servers" else d
clocks._sonder = lambda node: {"joignable": True, "convention": "realtime_tai", "phc2sys_unites": []}


def fake_exec(node, cmd, timeout=0, **k):
    envoyes.append(cmd)
    return 0, "applique\n", ""


clocks.node_driver.host_exec = fake_exec

clocks._discipline = lambda node: {"par": "moteur 2110", "phc2sys": True}
envoyes.clear()
r = clocks.appliquer_ntp_tai(34)
cas("REALTIME=TAI tenu par libmtl → refus", (False, 0), (r[0], len(envoyes)))

clocks._discipline = lambda node: {"par": "moteur 2110 (média) + chrony", "phc2sys": False}
envoyes.clear()
r = clocks.appliquer_ntp_tai(34)
cas("REALTIME=TAI laissé sans servo → chrony installé", (True, 1), (r[0], len(envoyes)))
cmd = envoyes[0] if envoyes else ""
cas("leapseclist posé (tai_offset maintenu)", True, "leapseclist" in cmd)
cas("sources du site posées", True,
    "server 10.0.0.1 iburst" in cmd and "server 10.0.0.2 iburst" in cmd and "bobi.sources" in cmd)
i_stop, i_drift = cmd.find("systemctl stop chrony"), cmd.find("rm -f /var/lib/chrony/chrony.drift")
i_raz, i_chrony = cmd.find("adjtimex"), cmd.find("systemctl start chrony")
cas("tick remis au nominal AVANT le démarrage de chrony", True, 0 <= i_raz < i_chrony)
# apt-get install DÉMARRE chrony : sans arrêt + driftfile effacé avant la remise au nominal, il
# ré-applique au redémarrage la fréquence faussée qu'il a lue puis écrite (dl360-1, 2026-10-17).
cas("chrony arrêté et driftfile effacé AVANT la remise au nominal", True,
    0 <= i_stop < i_raz and 0 <= i_drift < i_raz)
cas("aucun `restart` (il réécrirait le driftfile)", False, "restart chrony" in cmd)

# ── 4) la commande de remise au nominal compile ─────────────────────────────────────────────────
m = re.search(r"python3 -c '([^']*)'", clocks._RAZ_TICK_SH)
try:
    compile(m.group(1), "<raz_tick>", "exec")
    compile_ok = True
except Exception as e:
    print("   ", e)
    compile_ok = False
cas("snippet adjtimex : Python valide, sans apostrophe interne", True, bool(m) and compile_ok)
cas("snippet adjtimex : ADJ_FREQUENCY|ADJ_TICK (0x4002)", True, "0x4002" in clocks._RAZ_TICK_SH)

print("\n" + ("TOUT OK" if ok_total else "ÉCHEC"))
sys.exit(0 if ok_total else 1)
