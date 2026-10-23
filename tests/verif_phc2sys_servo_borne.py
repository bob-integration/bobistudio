#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Servo phc2sys de libmtl (CLOCK_REALTIME ← PHC) : il ne doit plus pouvoir s'emballer.

Contexte (dl360-1) : +396 s le 2026-10-16 (tick 10122), −455 s le 2026-10-17 (tick 9840,
−16 ms/s), « adj system time freq fail » à chaque cycle, sortie seulement par relance du moteur.
Le servo amont n'a ni borne, ni anti-windup, ni nouveau saut ; le noyau refuse un tick hors de
±10 % et l'intégrateur part. `plugins/2110_io/docker/patch_phc2sys_servo_bound.py` corrige.

Ce que la garde vérifie, hors ligne :
  1. un MODÈLE du servo, même arithmétique que mt_ptp.c (pi_sample, conversion tick + freq,
     round(), règles de kernel/time/ntp.c) : en régime sain le patch ne change RIEN (commandes
     identiques cycle à cycle) ; une excursion du PHC emballe le servo amont et pas le servo
     patché. Les constantes du modèle (seuil de re-saut, bornes) sont LUES dans le patch ;
  2. la STRUCTURE du patch : re-saut testé AVANT pi_sample, intégration annulée quand la commande
     est bornée ou refusée, bornes égales à celles du noyau, journal limité ;
  3. les trois endroits d'un patch libmtl (mémoire mtl-image-patch-three-places) : fichier,
     Dockerfile (COPY + exécution APRÈS patch_ptp_adjust_freq), liste de staging d'images.py.
Le banc `tools/bancs/banc_phc2sys_servo.py` rejoue les mêmes scénarios sur le VRAI code C."""
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCK = os.path.join(RACINE, "plugins", "2110_io", "docker")
PATCH = os.path.join(DOCK, "patch_phc2sys_servo_bound.py")

ok_total = True


def cas(nom, cond, detail=""):
    global ok_total
    ok_total &= bool(cond)
    print("%s %s%s" % ("OK  " if cond else "✕ KO", nom, ("  — " + detail) if detail else ""))


if not os.path.exists(PATCH):
    print("    (sous-module 2110_io absent : garde sans objet)")
    sys.exit(0)
src = open(PATCH).read()

# ── constantes lues dans le patch ────────────────────────────────────────────────────────────
m = re.search(r"#define BOBI_PHC2SYS_STEP_NS \((\d+) \* (\d+)\)", src)
STEP_NS = int(m.group(1)) * int(m.group(2)) if m else None
cas("seuil de re-saut déclaré (1 ms)", STEP_NS == 1_000_000, str(STEP_NS))
cas("bornes = règle du noyau (900000/hz .. 1100000/hz)",
    "900000 / hz - nominal" in src and "1100000 / hz - nominal" in src)

# ── modèle : noyau + horloges ────────────────────────────────────────────────────────────────
HZ, NOMINAL = 100, 10000


class Monde:
    def __init__(self):
        self.r = 1000e9 - 2.5e6      # CLOCK_REALTIME (ns) — époque basse : le float garde la ns
        self.p = 1000e9              # PHC (ns)
        self.tick, self.freq_ppm = NOMINAL, 0.0
        self.xtal_ppm = -11.0        # quartz système (dl360-1 : freq ≈ +11 ppm)
        self.phc_ppm = 0.0
        self.refus = 0
        self.t = 0.0

    def avance(self, dt):
        rate = 1 + (self.tick - NOMINAL) / NOMINAL + (self.freq_ppm + self.xtal_ppm) * 1e-6
        self.r += dt * 1e9 * rate
        self.p += dt * 1e9 * (1 + self.phc_ppm * 1e-6)
        self.t += dt

    # kernel/time/ntp.c : tick hors plage → EINVAL, RIEN n'est appliqué ; freq écrêtée ±500 ppm
    def adjtime_freq(self, tick, freq_scaled):
        if tick < 900000 // HZ or tick > 1100000 // HZ:
            self.refus += 1
            return -1
        self.tick = tick
        self.freq_ppm = max(-500.0, min(500.0, freq_scaled / 65536.0))
        return 0

    def adjtime_step(self, delta_ns):
        self.r += delta_ns


def c_round(x):                     # round() de la libc : demi-entiers loin de zéro
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def adj_freq(monde, ppb):           # ptp_adj_system_clock_freq
    tick = c_round(ppb / 1e3 / HZ) + NOMINAL
    ppb -= 1e3 * HZ * (tick - NOMINAL)
    return monde.adjtime_freq(tick, int(ppb * 65.536))


class Servo:                        # struct mt_pi_servo + pi_sample
    def __init__(self):
        self.o, self.l, self.drift, self.count = [0.0, 0.0], [0.0, 0.0], 0.0, 0

    def sample(self, offset, local):
        if self.count == 0:
            self.o[0], self.l[0], self.count = offset, local, 1
            return 0.0, "UNLOCKED"
        if self.count == 1:
            self.o[1], self.l[1], self.count = offset, local, 2
            return 0.0, "UNLOCKED"
        if self.count == 2:
            self.drift += (self.o[1] - self.o[0]) / (self.l[1] - self.l[0])
            self.count = 3
            return 0.0, "UNLOCKED"
        if self.count == 3:
            self.count = 4
            return 0.0, "JUMP"
        self.drift += 0.7 * offset
        return 0.3 * offset + self.drift, "LOCKED"


def bornes():
    return (1e3 * HZ * (900000 // HZ - NOMINAL), 1e3 * HZ * (1100000 // HZ - NOMINAL))


def phc2sys(monde, servo, patche, mut=()):
    """Un cycle de phc2sys_adjust. Renvoie la commande de fréquence appliquée (None sinon)."""
    offset = int(monde.r) - int(monde.p)
    drift_avant = servo.drift
    if patche and "restep" not in mut and servo.count >= 4 and abs(offset) >= STEP_NS:
        monde.adjtime_step(-offset)
        servo.__init__()
        adj_freq(monde, 0.0)
        return None
    ppb, state = servo.sample(offset, int(monde.r))
    if state == "JUMP":
        monde.adjtime_step(-offset)
    elif state == "LOCKED":
        want = -ppb
        if not patche:
            adj_freq(monde, want)
            return want
        lo, hi = bornes()
        cmd = want if "clamp" in mut else max(lo, min(hi, want))
        ret = adj_freq(monde, cmd)
        if (cmd != want or ret < 0) and "windup" not in mut:
            servo.drift = drift_avant
        return cmd
    return None


def scenario(nom, patche, mut=()):
    w, s, dt = Monde(), Servo(), 0.125
    cmds = []

    def run(secs):
        for _ in range(int(round(secs / dt))):
            w.avance(dt)
            cmds.append(phc2sys(w, s, patche, mut))
    run(60)
    if nom == "B":                  # saut de phase du PHC de 200 ms
        w.p += 200e6
    if nom == "E":                  # excursion de fréquence du PHC : +12 % pendant 2 s
        w.phc_ppm = 120000
        run(2)
        w.phc_ppm = 0
    run(600)
    return w, cmds


def converge(w):
    return abs(w.r - w.p) < 1e3 and w.tick == NOMINAL and abs(w.freq_ppm - 11) < 1


# 1) régime sain : commandes IDENTIQUES cycle à cycle
wa, ca = scenario("A", False)
wb, cb = scenario("A", True)
cas("A nominal : le servo amont converge (|offset| < 1 µs, tick 10000, +11 ppm)", converge(wa))
cas("A nominal : le patch ne change AUCUNE commande (régime sain)", ca == cb,
    "%d cycles" % len(ca))

# 2) excursions : amont emballé, patché convergé
for sc, quoi in (("B", "saut de phase du PHC de 200 ms"),
                 ("E", "excursion du PHC +12 % × 2 s")):
    wa, _ = scenario(sc, False)
    wp, _ = scenario(sc, True)
    cas("%s %s : AMONT emballé (refus noyau en continu)" % (sc, quoi), wa.refus > 1000,
        "refus=%d tick=%d offset=%.1f ms" % (wa.refus, wa.tick, (wa.r - wa.p) / 1e6))
    cas("%s %s : PATCHÉ convergé, zéro refus" % (sc, quoi), converge(wp) and wp.refus == 0,
        "refus=%d tick=%d offset=%.6f ms" % (wp.refus, wp.tick, (wp.r - wp.p) / 1e6))
wa, _ = scenario("E", False)
cas("E : l'amont reproduit la SIGNATURE de dl360-1 (tick figé ≠ 10000, offset > 1 s)",
    wa.tick != NOMINAL and abs(wa.r - wa.p) > 1e9, "tick=%d" % wa.tick)

# 2 bis) défense en profondeur : chaque mécanisme seul suffit sur ces scénarios — c'est voulu —
# mais retirer le re-saut ET la borne doit casser (sinon la garde ne mord pas).
w, _ = scenario("E", True, mut=("restep", "clamp"))
cas("mutation re-saut + borne retirés → la garde MORD (refus revenus)", w.refus > 0,
    "refus=%d" % w.refus)

# ── 3) structure du patch ────────────────────────────────────────────────────────────────────
cas("marqueur du patch", "bobi.studio: phc2sys servo borne" in src)
i_restep = src.find("if (bobi_phc2sys_restep(ptp, offset))")
i_sample = src.find("ppb = pi_sample(&ptp->phc2sys.servo, offset, t_sys, &state);\\n\"\n"
                    "              \"    }")
cas("re-saut testé AVANT pi_sample", 0 <= i_restep < i_sample)
cas("anti-windup : drift ramené à sa valeur d'avant pi_sample",
    "p->servo.drift = drift_before;" in src and "if (clamped || ret < 0)" in src)
cas("re-saut : servo réinitialisé et vitesse noyau au nominal",
    "memset(&p->servo, 0, sizeof(p->servo));" in src and "ptp_adj_system_clock_freq(ptp, 0.0);" in src)
cas("plus aucun appel non borné en LOCKED (contrôle de sortie du patch)",
    'if "ptp_adj_system_clock_freq(ptp, -ppb)" in c:' in src)
cas("journal limité (période de 10 s) et en anglais",
    "BOBI_PHC2SYS_LOG_PERIOD_NS (10ull * NS_PER_S)" in src and "beyond step threshold" in src
    and "integrator held" in src)
cas("ancres vérifiées UNIQUES (pas seulement présentes)", src.count("unique(") >= 4)

# ── 4) les trois endroits ────────────────────────────────────────────────────────────────────
df = open(os.path.join(DOCK, "Dockerfile")).read()
cas("Dockerfile : COPY", "COPY patch_phc2sys_servo_bound.py /tmp/patch_phc2sys_servo_bound.py" in df)
i_af = df.find("python3 /tmp/patch_ptp_adjust_freq.py")
i_me = df.find("python3 /tmp/patch_phc2sys_servo_bound.py")
cas("Dockerfile : exécuté APRÈS patch_ptp_adjust_freq", 0 <= i_af < i_me)
img = open(os.path.join(RACINE, "app", "routes", "images.py")).read()
cas("images.py : dans la liste de staging",
    '("plugins/2110_io/docker/patch_phc2sys_servo_bound.py", "patch_phc2sys_servo_bound.py")' in img)

print("\n" + ("TOUT OK" if ok_total else "ÉCHEC"))
sys.exit(0 if ok_total else 1)
