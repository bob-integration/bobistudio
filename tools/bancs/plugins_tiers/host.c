// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// host.c — hôte CO-PROCESSUS synchrone (banc plugins tiers, étape 0).
// Usage : host <shm_name> <uri> <block> <cpu|-1> <fifo_prio|0> [params] [slow_us slow_every crash_at]
//         [async_offset_us]  (>= 0 : mode ASYNC — l'hôte tourne sur SA propre horloge de bloc, décalée
//          de async_offset_us sur la grille du moteur, comme un conteneur séparé branché par flux)
// Attend un bloc (futex sur req), le traite par libplugcore (lilv), rend (done + futex wake).
#define _GNU_SOURCE
#include "shm.h"
#include <fcntl.h>
#include <linux/futex.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

int pc_open(const char *uri, double fs, int block, const char *params);
void pc_process(const float *, const float *, float *, float *, int);
int pc_latency(void);
const char *pc_error(void);
void pc_set_fault(long, long, long);

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}

int main(int argc, char **argv) {
    if (argc < 6) { fprintf(stderr, "usage\n"); return 2; }
    const char *name = argv[1], *uri = argv[2];
    int B = atoi(argv[3]), cpu = atoi(argv[4]), prio = atoi(argv[5]);
    const char *params = argc > 6 ? argv[6] : "";
    if (argc > 9) pc_set_fault(atol(argv[7]), atol(argv[8]), atol(argv[9]));
    long async_off = argc > 10 ? atol(argv[10]) : -1;
    if (cpu >= 0) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu, &s); sched_setaffinity(0, sizeof s, &s); }
    int fd = shm_open(name, O_RDWR, 0600);
    if (fd < 0) { perror("shm_open"); return 1; }
    struct shm *m = mmap(NULL, sizeof *m, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m == MAP_FAILED) { perror("mmap"); return 1; }
    uint64_t t0 = now_ns();
    if (pc_open(uri, 48000, B, params) != 0) { fprintf(stderr, "host: %s\n", pc_error()); return 3; }
    m->host_init_ns = now_ns() - t0;
    mlockall(MCL_CURRENT | MCL_FUTURE);
    if (prio > 0) { struct sched_param sp = {prio}; sched_setscheduler(0, SCHED_FIFO, &sp); }
    // Chauffe hors ligne : premier run() souvent coûteux (allocations paresseuses du plugin).
    static float wl[2][SHM_MAXB], wo[2][SHM_MAXB];
    for (int i = 0; i < 20; i++) pc_process(wl[0], wl[1], wo[0], wo[1], B);
    m->latency = pc_latency();              // port de latence : valide après un run()
    m->host_pid = getpid();
    uint32_t last = atomic_load(&m->req);
    atomic_store(&m->done, last);           // aucun bloc en vol pour ce nouvel hôte (out_seq inchangé)
    m->host_ready_ns = now_ns();
    atomic_store(&m->host_ready, 1);
    if (async_off >= 0) {                   // ASYNC : réveil par horloge, pas par le moteur
        while (!m->base_ns) usleep(100);    // le moteur publie sa grille au démarrage de sa boucle
        uint64_t P = m->period_ns, t0 = m->base_ns + async_off * 1000ull, tn = now_ns();
        uint64_t t = tn <= t0 ? t0 : t0 + ((tn - t0) / P + 1) * P;
        for (;;) {
            struct timespec ts = {t / 1000000000ull, t % 1000000000ull};
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
            t += P;
            uint32_t r = atomic_load_explicit(&m->req, memory_order_acquire);
            if (r == last) continue;
            uint64_t tw = now_ns();
            m->host_wake_ns = tw;
            pc_process(m->in[0], m->in[1], m->out[0], m->out[1], m->n);
            m->host_proc_ns = now_ns() - tw;
            atomic_store_explicit(&m->out_seq, r, memory_order_release);
            atomic_store_explicit(&m->done, r, memory_order_release);
            last = r;
        }
    }
    for (;;) {
        uint32_t r;
        while ((r = atomic_load_explicit(&m->req, memory_order_acquire)) == last)
            syscall(SYS_futex, &m->req, FUTEX_WAIT, last, NULL, NULL, 0);
        uint64_t tw = now_ns();
        m->host_wake_ns = tw;
        pc_process(m->in[0], m->in[1], m->out[0], m->out[1], m->n);
        m->host_proc_ns = now_ns() - tw;
        atomic_store_explicit(&m->out_seq, r, memory_order_release);
        atomic_store_explicit(&m->done, r, memory_order_release);
        syscall(SYS_futex, &m->done, FUTEX_WAKE, 1, NULL, NULL, 0);
        last = r;
    }
}
