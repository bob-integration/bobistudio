#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc du servo phc2sys de libmtl — sur le VRAI code C, contre un noyau simulé.

Pourquoi un banc en plus de la garde `tests/verif_phc2sys_servo_borne.py` : la garde rejoue le
servo dans un modèle Python ; ce banc compile le code de `mt_ptp.c` LUI-MÊME (pi_sample,
ptp_adj_system_clock_time/freq, phc2sys_adjust, et les aides du patch s'il est appliqué) et le fait
tourner contre un noyau simulé qui applique les MÊMES règles que kernel/time/ntp.c :
  · ADJ_TICK hors de [900000/USER_HZ ; 1100000/USER_HZ] → clock_adjtime rend -1 (EINVAL) et RIEN
    n'est appliqué (ni tick ni fréquence) — l'horloge garde la dernière vitesse acceptée ;
  · ADJ_FREQUENCY écrêté à ±500 ppm ; ADJ_SETOFFSET = saut de phase.
Il exige une arborescence libmtl (non versionnée : récupérée au build de l'image) :

    curl -sL https://codeload.github.com/OpenVisualCloud/Media-Transport-Library/tar.gz/<MTL_REF> \\
      | tar xz --strip-components=1 -C <dir>
    cd <dir> && for p in <patchs du Dockerfile, DANS L'ORDRE>; do python3 -I .../patch_$p.py; done

Usage : banc_phc2sys_servo.py <dir_avant> <dir_apres> [step_ms]
        <dir_avant> = arborescence patchée SANS patch_phc2sys_servo_bound, <dir_apres> = AVEC.
        Seuls mt_ptp.c et mt_main.h sont lus (sous lib/src/ ou à la racine du dossier).
Verdict : en régime nominal les deux convergent pareil (le patch ne touche pas un servo sain) ;
l'excursion du PHC qui emballe le code « avant » n'emballe pas le code « après ».
Mesures du 2026-10-17 : cf. mémoire dl360-clock-disciplined-by-libmtl-phc2sys."""
import os
import re
import subprocess
import sys
import tempfile

HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>
#include <inttypes.h>
#include <errno.h>
#include <time.h>
#include <sys/timex.h>

#define NS_PER_S 1000000000ull
#define RTE_MAX(a, b) ((a) > (b) ? (a) : (b))
#define RTE_MIN(a, b) ((a) < (b) ? (a) : (b))
#define MTL_MAY_UNUSED(x) (void)(x)
static int g_warn = 0;
#define dbg(...) do {} while (0)
#define info(...) do {} while (0)
#define notice(...) do {} while (0)
#define warn(...) do { g_warn++; if (getenv("BANC_VERBOSE")) fprintf(stderr, __VA_ARGS__); } while (0)
#define err(...) do {} while (0)

enum servo_state { UNLOCKED, JUMP, LOCKED };
%(structs)s
struct mt_ptp_impl { int port; int port_id; struct mt_phc2sys_impl phc2sys; };

/* ── noyau + horloges simulés ── époque basse : un double garde la nanoseconde ── */
static double T_true, R_ns, P_ns;   /* temps vrai (s), CLOCK_REALTIME (ns), PHC (ns) */
static long K_tick = 10000;         /* adjtimex tick, USER_HZ = 100 */
static double K_freq_ppm = 0;       /* adjtimex freq (ppm) */
static double SYS_XTAL_PPM = -11.0; /* erreur propre du quartz système (dl360-1 : freq ≈ +11 ppm) */
static double PHC_RATE_PPM = 0.0;   /* excursion de fréquence du PHC */
static long g_refused = 0;

static double rt_rate(void) {
  return 1.0 + (K_tick - 10000) / 10000.0 + (K_freq_ppm + SYS_XTAL_PPM) * 1e-6;
}
static void advance(double dt) {
  R_ns += dt * 1e9 * rt_rate();
  P_ns += dt * 1e9 * (1.0 + PHC_RATE_PPM * 1e-6);
  T_true += dt;
}
static uint64_t mt_timespec_to_ns(const struct timespec* ts) {
  return (uint64_t)ts->tv_sec * NS_PER_S + ts->tv_nsec;
}
static int sim_clock_gettime(clockid_t id, struct timespec* ts) {
  (void)id;
  uint64_t v = (uint64_t)R_ns;
  ts->tv_sec = v / NS_PER_S;
  ts->tv_nsec = v %% NS_PER_S;
  return 0;
}
#define clock_gettime sim_clock_gettime
static int sim_clock_adjtime(clockid_t id, struct timex* tx) {
  (void)id;
  if ((tx->modes & ADJ_TICK) && (tx->tick < 900000 / 100 || tx->tick > 1100000 / 100)) {
    g_refused++;
    errno = EINVAL;
    return -1;
  }
  if (tx->modes & ADJ_SETOFFSET) R_ns += (double)tx->time.tv_sec * 1e9 + (double)tx->time.tv_usec;
  if (tx->modes & ADJ_TICK) K_tick = tx->tick;
  if (tx->modes & ADJ_FREQUENCY) {
    double f = tx->freq / 65536.0;
    K_freq_ppm = f > 500 ? 500 : (f < -500 ? -500 : f);
  }
  return 0;
}
#define clock_adjtime sim_clock_adjtime
static uint64_t mt_get_monotonic_time(void) { return (uint64_t)(T_true * 1e9); }
static void ptp_timesync_lock(struct mt_ptp_impl* p) { (void)p; }
static void ptp_timesync_unlock(struct mt_ptp_impl* p) { (void)p; }
static uint64_t ptp_timesync_read_time_no_lock(struct mt_ptp_impl* p) { (void)p; return (uint64_t)P_ns; }

/* ── code libmtl, extrait tel quel ── */
%(code)s

/* ── scénarios ── */
static struct mt_ptp_impl ptp;
static void reset(void) {
  memset(&ptp, 0, sizeof(ptp));
  ptp.phc2sys.realtime_hz = 100;
  ptp.phc2sys.realtime_nominal_tick = 10000;
  T_true = 1000.0; P_ns = 1000e9; R_ns = P_ns - 2.5e6; K_tick = 10000; K_freq_ppm = 0;
  PHC_RATE_PPM = 0; g_refused = 0; g_warn = 0;
}
static void run(double secs, double dt) {
  for (double t = 0; t < secs - 1e-9; t += dt) { advance(dt); phc2sys_adjust(&ptp); }
}
static void report(const char* nom) {
  printf("%%s offset_ms=%%.6f tick=%%ld freq_ppm=%%.2f refused=%%ld warn=%%d\n", nom,
         (R_ns - P_ns) / 1e6, K_tick, K_freq_ppm, g_refused, g_warn);
}
int main(int argc, char** argv) {
  double dt = 0.125;                                   /* logSyncInterval -3 */
  double step_ms = argc > 1 ? atof(argv[1]) : 200.0;
  /* A — nominal : acquisition depuis 2,5 ms d'écart, quartz −11 ppm */
  reset(); run(120, dt); report("A_nominal");
  /* B — saut de phase du PHC de step_ms une fois verrouillé */
  reset(); run(60, dt); P_ns += step_ms * 1e6; run(600, dt); report("B_phc_step");
  /* C — mesure PTP aberrante appliquée au PHC puis compensée au cycle suivant */
  reset(); run(60, dt); P_ns += step_ms * 1e6; run(dt, dt); P_ns -= step_ms * 1e6; run(600, dt);
  report("C_phc_outlier_pair");
  /* D — excursion de fréquence du PHC : +3 %% pendant 1 s, puis retour */
  reset(); run(60, dt); PHC_RATE_PPM = 30000; run(1, dt); PHC_RATE_PPM = 0; run(600, dt);
  report("D_phc_freq_kick");
  /* E — excursion de fréquence du PHC de +12 %% pendant 2 s : reproduit la SIGNATURE vue sur
   * dl360-1 (tick figé hors nominal, refus à chaque cycle, offset qui croît sans fin) */
  reset(); run(60, dt); PHC_RATE_PPM = 120000; run(2, dt); PHC_RATE_PPM = 0; run(600, dt);
  report("E_phc_freq_runaway");
  return 0;
}
'''


def extract(srcdir):
    def find(name):
        for cand in (os.path.join(srcdir, "lib", "src", name), os.path.join(srcdir, name)):
            if os.path.exists(cand):
                return open(cand).read()
        sys.exit("introuvable : %s sous %s" % (name, srcdir))
    c, h = find("mt_ptp.c"), find("mt_main.h")
    m1 = re.search(r"struct mt_pi_servo \{.*?\};", h, re.S)
    m2 = re.search(r"struct mt_phc2sys_impl \{.*?\};", h, re.S)
    code = c[c.index("static inline double pi_sample("):
             c.index("static inline int ptp_timesync_read_tx_time(")]
    # Branches Windows retirées (#ifndef WINDOWSENV … #else … #endif) : elles ne compilent pas ici.
    code = re.sub(r"#else[^\n]*\n.*?#endif[^\n]*\n", "#endif\n", code, flags=re.S)
    code = code.replace("#ifndef WINDOWSENV", "#if 1")
    return m1.group(0) + "\n" + m2.group(0), code


def build_and_run(srcdir, tag, step_ms):
    structs, code = extract(srcdir)
    d = tempfile.mkdtemp(prefix="banc_phc2sys_")
    cf, exe = os.path.join(d, "h.c"), os.path.join(d, "h")
    open(cf, "w").write(HARNESS % {"structs": structs, "code": code})
    r = subprocess.run(["gcc", "-O2", "-Wall", "-Wno-unused-function", "-o", exe, cf, "-lm"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit("compilation %s : %s" % (tag, r.stderr[-3000:]))
    out = subprocess.run([exe, str(step_ms)], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        nom, *kv = line.split()
        res[nom] = {k: float(v) for k, v in (x.split("=") for x in kv)}
        print("%-6s %s" % (tag, line))
    return res


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    step = float(sys.argv[3]) if len(sys.argv) > 3 else 200.0
    av, ap = build_and_run(sys.argv[1], "avant", step), build_and_run(sys.argv[2], "apres", step)
    ok = True

    def cas(nom, cond):
        nonlocal ok
        ok &= bool(cond)
        print("%s %s" % ("OK  " if cond else "✕ KO", nom))

    def converge(r):
        return abs(r["offset_ms"]) < 1e-3 and r["tick"] == 10000 and abs(r["freq_ppm"] - 11) < 1

    cas("A nominal : les deux convergent (|offset| < 1 µs, tick 10000, freq ≈ +11 ppm)",
        converge(av["A_nominal"]) and converge(ap["A_nominal"]))
    for sc in ("B_phc_step", "C_phc_outlier_pair"):
        # Emballement = le noyau refuse encore après 10 min (> 1000 cycles refusés) : l'intégrateur
        # est parti et ne revient plus. En C l'horloge peut rester à l'heure PAR CHANCE (la vitesse
        # figée est la bonne) — c'est l'intégrateur qui est perdu, pas encore l'heure.
        cas("%s (%g ms) : AVANT le patch, emballement (refus noyau en continu)" % (sc, step),
            av[sc]["refused"] > 1000)
        cas("%s (%g ms) : APRÈS le patch, convergé, zéro refus" % (sc, step),
            converge(ap[sc]) and ap[sc]["refused"] == 0)
    cas("D excursion de fréquence : APRÈS le patch, convergé, zéro refus",
        converge(ap["D_phc_freq_kick"]) and ap["D_phc_freq_kick"]["refused"] == 0)
    e_av, e_ap = av["E_phc_freq_runaway"], ap["E_phc_freq_runaway"]
    cas("E +12 % × 2 s : AVANT, signature dl360-1 (tick figé ≠ 10000, refus, offset > 1 s)",
        e_av["tick"] != 10000 and e_av["refused"] > 1000 and abs(e_av["offset_ms"]) > 1000)
    cas("E +12 % × 2 s : APRÈS le patch, convergé, zéro refus",
        converge(e_ap) and e_ap["refused"] == 0)
    print("\n" + ("TOUT OK" if ok else "ÉCHEC"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
