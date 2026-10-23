// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// engine.c — « moteur » FACTICE du banc plugins tiers, étape 0
// (docs/chantiers/CONSOLE_PLUGINS_TIERS_BANC0.md). Produit des blocs de B échantillons stéréo float32
// au rythme de B/48000 s (240 → 5 ms, comme la console) sur horloge absolue, et fait traiter chaque
// bloc par un plugin LV2 selon trois modes :
//   inproc : libplugcore.so chargé par dlopen() DANS ce processus (un plantage du plugin tue le moteur)
//   coproc : hôte séparé (./host), mémoire partagée + futex, ÉCHÉANCE absolue ; échéance dépassée,
//            hôte occupé ou absent → le bloc passe SEC (même latence) ; hôte mort → relancé
//   async  : hôte séparé sur sa PROPRE horloge (simulation de l'insert par flux) : le retour du bloc n
//            est lu au bloc n+1 — un bloc de latence structurelle
// Imprime un résumé JSON sur stdout. Aucune dépendance au reste de Bobi.Studio.
#define _GNU_SOURCE
#include "shm.h"
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

// --- options ---
static const char *mode = "coproc", *uri = "null", *params = "";
static int B = 240, engine_cpu = -1, host_cpu = -1, prio = 0, host_prio = -1, rt_wd_blocks = 0, nkills_rt, hang_ms = 0, async_off_us = 2500;
static double seconds = 10, deadline_us = 2000, spin_us = 0, kill_every_s = 0;
static long slow_us, slow_every, crash_at;

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}

// --- co-processus : supervision ---
static struct shm *m;
static char shm_name[64];
static volatile int stop_all;
static volatile pid_t host_pid;
static _Atomic uint64_t t_last_submit;
#define MAXEV 4096
static uint64_t death_ns[MAXEV], restart_ns[MAXEV], init_ns[MAXEV];
static int ndeath, nrestart, ninit, nkills_watchdog, nkills_chaos;
static volatile int restart_pending;
static double host_cpu_s_dead;              // CPU consommé par les hôtes déjà morts

static pid_t spawn_host(void) {
    char b[16], c[16], p[16], su[24], se[24], ca[24], ao[24];
    snprintf(b, sizeof b, "%d", B); snprintf(c, sizeof c, "%d", host_cpu); snprintf(p, sizeof p, "%d", host_prio >= 0 ? host_prio : prio);
    snprintf(su, sizeof su, "%ld", slow_us); snprintf(se, sizeof se, "%ld", slow_every);
    snprintf(ca, sizeof ca, "%ld", crash_at);
    snprintf(ao, sizeof ao, "%d", strcmp(mode, "async") ? -1 : async_off_us);
    char *argv[] = {"./host", shm_name, (char *)uri, b, c, p, (char *)params, su, se, ca, ao, NULL};
    pid_t pid;
    if (posix_spawn(&pid, "./host", NULL, NULL, argv, environ) != 0) return -1;
    return pid;
}

static void *supervisor(void *a) {
    (void)a;
    for (;;) {
        pid_t pid = spawn_host();
        host_pid = pid;
        int st;
        struct rusage ru;
        if (wait4(pid, &st, 0, &ru) < 0) return NULL;
        host_cpu_s_dead += ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 + ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6;
        atomic_store(&m->host_ready, 0);
        if (stop_all) return NULL;
        if (ndeath < MAXEV) death_ns[ndeath++] = now_ns();
        restart_pending = 1;
        if (WIFEXITED(st) && WEXITSTATUS(st) == 3) { fprintf(stderr, "host: plugin load failed\n"); stop_all = 2; return NULL; }
    }
}

// Chien de garde : un hôte qui garde un bloc plus de hang_ms est tué (boucle infinie, blocage).
// Chaos : tue l'hôte (SIGKILL) toutes les kill_every_s secondes — équivalent de `kill -9` externe.
static void *watchdog(void *a) {
    (void)a;
    uint64_t next_kill = now_ns() + (uint64_t)(kill_every_s * 1e9);
    while (!stop_all) {
        struct timespec ts = {0, 1000000};
        nanosleep(&ts, NULL);
        // Âge lu APRÈS l'état et en signé : sinon une soumission entre la lecture de l'horloge et celle
        // de req donne un âge négatif → énorme en non signé → faux « hôte bloqué » (vu au banc).
        int busy = atomic_load(&m->host_ready) && atomic_load(&m->done) != atomic_load(&m->req);
        uint64_t t = now_ns();
        int64_t age = (int64_t)(t - t_last_submit);
        if (hang_ms && busy && age > (int64_t)hang_ms * 1000000) {
            kill(host_pid, SIGKILL); nkills_watchdog++;
            while (atomic_load(&m->host_ready) && !stop_all) nanosleep(&ts, NULL);
        }
        if (kill_every_s > 0 && t >= next_kill) {
            if (atomic_load(&m->host_ready)) { kill(host_pid, SIGKILL); nkills_chaos++; }
            next_kill = t + (uint64_t)(kill_every_s * 1e9);
        }
        if (atomic_load(&m->host_ready) && m->host_ready_ns && ninit < MAXEV) {
            static uint64_t seen;
            if (m->host_ready_ns != seen) { seen = m->host_ready_ns; init_ns[ninit++] = m->host_init_ns; }
        }
    }
    return NULL;
}

static long futex_wait_abs(_Atomic uint32_t *addr, uint32_t val, uint64_t abs_ns) {
    struct timespec ts = {abs_ns / 1000000000ull, abs_ns % 1000000000ull};
    return syscall(SYS_futex, addr, FUTEX_WAIT_BITSET, val, &ts, NULL, FUTEX_BITSET_MATCH_ANY);
}

// --- statistiques ---
static int cmp_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y;
}
static void stats(const char *name, uint64_t *v, long n, int last) {
    if (n <= 0) { printf("  \"%s\": null%s\n", name, last ? "" : ","); return; }
    qsort(v, n, sizeof *v, cmp_u64);
    double s = 0;
    for (long i = 0; i < n; i++) s += v[i];
#define P(q) (v[(long)((n - 1) * (q))] / 1000.0)
    printf("  \"%s\": {\"n\": %ld, \"mean_us\": %.2f, \"p50_us\": %.2f, \"p90_us\": %.2f, \"p99_us\": %.2f, "
           "\"p999_us\": %.2f, \"p9999_us\": %.2f, \"max_us\": %.2f}%s\n",
           name, n, s / n / 1000.0, P(0.5), P(0.9), P(0.99), P(0.999), P(0.9999), v[n - 1] / 1000.0, last ? "" : ",");
}

static double proc_cpu_s(pid_t pid, long *vcs, long *nvcs) {
    char path[64], buf[2048];
    snprintf(path, sizeof path, "/proc/%d/stat", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    if (!fgets(buf, sizeof buf, f)) { fclose(f); return -1; }
    fclose(f);
    char *p = strrchr(buf, ')');
    unsigned long ut, st;
    // champs après ')' : 3=state ... 14=utime 15=stime
    if (sscanf(p + 2, "%*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %lu %lu", &ut, &st) != 2) return -1;
    snprintf(path, sizeof path, "/proc/%d/status", pid);
    f = fopen(path, "r");
    *vcs = *nvcs = 0;
    while (f && fgets(buf, sizeof buf, f)) {
        sscanf(buf, "voluntary_ctxt_switches: %ld", vcs);
        sscanf(buf, "nonvoluntary_ctxt_switches: %ld", nvcs);
    }
    if (f) fclose(f);
    return (ut + st) / (double)sysconf(_SC_CLK_TCK);
}

int main(int argc, char **argv) {
    for (int i = 1; i + 1 < argc; i += 2) {
        const char *k = argv[i], *v = argv[i + 1];
        if (!strcmp(k, "--mode")) mode = v;
        else if (!strcmp(k, "--uri")) uri = v;
        else if (!strcmp(k, "--params")) params = v;
        else if (!strcmp(k, "--block")) B = atoi(v);
        else if (!strcmp(k, "--seconds")) seconds = atof(v);
        else if (!strcmp(k, "--deadline-us")) deadline_us = atof(v);
        else if (!strcmp(k, "--spin-us")) spin_us = atof(v);
        else if (!strcmp(k, "--engine-cpu")) engine_cpu = atoi(v);
        else if (!strcmp(k, "--host-cpu")) host_cpu = atoi(v);
        else if (!strcmp(k, "--fifo")) prio = atoi(v);
        else if (!strcmp(k, "--rt-watchdog-blocks")) rt_wd_blocks = atoi(v);  // le FIL AUDIO tue l'hôte
        else if (!strcmp(k, "--host-fifo")) host_prio = atoi(v);   // 0 = hôte en SCHED_OTHER
        else if (!strcmp(k, "--hang-ms")) hang_ms = atoi(v);
        else if (!strcmp(k, "--kill-every-s")) kill_every_s = atof(v);
        else if (!strcmp(k, "--slow-us")) slow_us = atol(v);
        else if (!strcmp(k, "--slow-every")) slow_every = atol(v);
        else if (!strcmp(k, "--crash-at")) crash_at = atol(v);
        else if (!strcmp(k, "--async-offset-us")) async_off_us = atoi(v);
        else { fprintf(stderr, "unknown option %s\n", k); return 2; }
    }
    if (B > SHM_MAXB) return 2;
    const uint64_t P = (uint64_t)B * 1000000000ull / 48000;
    const long N = (long)(seconds * 1e9 / P);
    int inproc = !strcmp(mode, "inproc"), async = !strcmp(mode, "async");

    // Tampons d'enregistrement pré-alloués (aucune allocation dans la boucle).
    uint64_t *rt = calloc(N, 8), *proc = calloc(N, 8), *ovh = calloc(N, 8), *late = calloc(N, 8),
             *wake = calloc(N, 8);
    long nrt = 0, nproc = 0, novh = 0, nlate = 0;
    long ok = 0, dry_late = 0, dry_busy = 0, dry_nohost = 0, overruns = 0, nan_out = 0;
    long dry_run = 0, max_dry_run = 0;
    static float inL[SHM_MAXB], inR[SHM_MAXB], outL[SHM_MAXB], outR[SHM_MAXB];
    double phase = 0, sink = 0;
    unsigned rng = 12345;

    void (*pc_process)(const float *, const float *, float *, float *, int) = NULL;
    int plat = 0;
    uint64_t inproc_init = 0;
    if (inproc) {
        void *h = dlopen("./libplugcore.so", RTLD_NOW | RTLD_LOCAL);
        if (!h) { fprintf(stderr, "%s\n", dlerror()); return 1; }
        int (*pc_open)(const char *, double, int, const char *) = dlsym(h, "pc_open");
        int (*pc_lat)(void) = dlsym(h, "pc_latency");
        const char *(*pc_err)(void) = dlsym(h, "pc_error");
        void (*pc_fault)(long, long, long) = dlsym(h, "pc_set_fault");
        pc_process = dlsym(h, "pc_process");
        uint64_t t0 = now_ns();
        if (pc_open(uri, 48000, B, params)) { fprintf(stderr, "plugin: %s\n", pc_err()); return 3; }
        inproc_init = now_ns() - t0;
        for (int i = 0; i < 20; i++) pc_process(inL, inR, outL, outR, B);
        plat = pc_lat();                   // port de latence : valide après un run()
        pc_fault(slow_us, slow_every, crash_at);
    } else {
        snprintf(shm_name, sizeof shm_name, "/plugbanc-%d", getpid());
        int fd = shm_open(shm_name, O_RDWR | O_CREAT | O_EXCL, 0600);
        if (fd < 0 || ftruncate(fd, sizeof *m)) { perror("shm"); return 1; }
        m = mmap(NULL, sizeof *m, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        memset(m, 0, sizeof *m);
        m->n = B;
        m->period_ns = P;
        pthread_t ts, tw;
        pthread_create(&ts, NULL, supervisor, NULL);
        pthread_create(&tw, NULL, watchdog, NULL);
        uint64_t t0 = now_ns();
        while (!atomic_load(&m->host_ready) && !stop_all && now_ns() - t0 < 30000000000ull) usleep(1000);
        if (!atomic_load(&m->host_ready)) { fprintf(stderr, "host never ready\n"); shm_unlink(shm_name); return 3; }
        plat = m->latency;
    }
    if (engine_cpu >= 0) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(engine_cpu, &s); sched_setaffinity(0, sizeof s, &s); }
    mlockall(MCL_CURRENT | MCL_FUTURE);
    if (prio > 0) {
        struct sched_param sp = {prio + 1};
        if (sched_setscheduler(0, SCHED_FIFO, &sp)) perror("sched_setscheduler(engine)");
    }
    struct rusage ru0;
    getrusage(RUSAGE_THREAD, &ru0);
    double host_cpu0 = 0; long hv0 = 0, hn0 = 0;
    if (!inproc) host_cpu0 = proc_cpu_s(host_pid, &hv0, &hn0);
    uint64_t base = (now_ns() / P + 2) * P, t_start = now_ns();
    if (!inproc) m->base_ns = base;
    uint32_t async_pending = 0;
    int busy_run = 0;            // async : numéro du bloc dont on attend le retour

    for (long n = 0; n < N && !stop_all; n++) {
        uint64_t tick = base + n * P;
        struct timespec ts = {tick / 1000000000ull, tick % 1000000000ull};
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
        uint64_t tw = now_ns();
        wake[n] = tw - tick;
        for (int i = 0; i < B; i++) {      // signal : sinus 1 kHz + bruit faible
            phase += 2 * M_PI * 1000.0 / 48000;
            rng = rng * 1103515245u + 12345u;
            float nz = ((rng >> 9) / 8388608.0f - 1.0f) * 0.01f;
            inL[i] = 0.5f * (float)sin(phase) + nz;
            inR[i] = 0.5f * (float)cos(phase) - nz;
        }
        int st;                            // 0 ok, 1 sec-échéance, 2 sec-occupé, 3 sec-hôte absent
        if (inproc) {
            uint64_t t0 = now_ns();
            pc_process(inL, inR, outL, outR, B);
            uint64_t d = now_ns() - t0;
            rt[nrt++] = d; proc[nproc++] = d;
            st = 0;
        } else if (async) {
            st = 3;
            if (async_pending && atomic_load_explicit(&m->out_seq, memory_order_acquire) == async_pending) {
                memcpy(outL, m->out[0], B * 4); memcpy(outR, m->out[1], B * 4);
                st = 0;
                uint64_t ready = m->host_wake_ns + m->host_proc_ns;
                rt[nrt++] = ready - (tick - P);  // délai soumission → retour disponible
                proc[nproc++] = m->host_proc_ns;
            } else if (async_pending) st = 1;
            if (!atomic_load(&m->host_ready)) { async_pending = 0; }
            else if (atomic_load(&m->done) == atomic_load(&m->req)) {
                memcpy(m->in[0], inL, B * 4); memcpy(m->in[1], inR, B * 4);
                uint32_t s = atomic_load(&m->req) + 1;
                t_last_submit = now_ns();
                atomic_store_explicit(&m->req, s, memory_order_release);
                async_pending = s;
            } else { if (st == 3) st = 2; async_pending = 0; }
            if (st) { memcpy(outL, inL, B * 4); memcpy(outR, inR, B * 4); }
        } else {
            if (!atomic_load_explicit(&m->host_ready, memory_order_acquire)) st = 3;
            else if (atomic_load(&m->done) != atomic_load(&m->req)) {
                st = 2;
                // Chien de garde DANS le fil temps réel : ne dépend d'aucun fil SCHED_OTHER (qu'un hôte
                // SCHED_FIFO en boucle sur le même cœur affamerait). kill() ne bloque pas.
                if (rt_wd_blocks && ++busy_run >= rt_wd_blocks) { kill(host_pid, SIGKILL); nkills_rt++; busy_run = 0; }
            } else {
                busy_run = 0;
                memcpy(m->in[0], inL, B * 4); memcpy(m->in[1], inR, B * 4);
                uint32_t s = atomic_load(&m->req) + 1;
                uint64_t tsub = now_ns(), dl = tw + (uint64_t)(deadline_us * 1000);
                t_last_submit = tsub;
                atomic_store_explicit(&m->req, s, memory_order_release);
                syscall(SYS_futex, &m->req, FUTEX_WAKE, 1, NULL, NULL, 0);
                uint32_t d;
                uint64_t spin_end = tsub + (uint64_t)(spin_us * 1000);
                while ((d = atomic_load_explicit(&m->done, memory_order_acquire)) != s && now_ns() < spin_end)
                    __builtin_ia32_pause();
                while ((d = atomic_load_explicit(&m->done, memory_order_acquire)) != s) {
                    if (now_ns() >= dl) break;
                    futex_wait_abs(&m->done, d, dl);
                }
                uint64_t tret = now_ns();
                if (d == s && atomic_load_explicit(&m->out_seq, memory_order_acquire) == s) {
                    memcpy(outL, m->out[0], B * 4); memcpy(outR, m->out[1], B * 4);
                    st = 0;
                    uint64_t hp = m->host_proc_ns;
                    rt[nrt++] = tret - tsub; proc[nproc++] = hp;
                    ovh[novh++] = (tret - tsub) > hp ? (tret - tsub) - hp : 0;
                } else st = 1;
            }
            if (st) { memcpy(outL, inL, B * 4); memcpy(outR, inR, B * 4); }   // SEC
        }
        if (st == 0) {
            ok++;
            if (restart_pending && nrestart < MAXEV) { restart_ns[nrestart++] = now_ns() - death_ns[ndeath - 1]; restart_pending = 0; }
            if (dry_run > max_dry_run) max_dry_run = dry_run;
            dry_run = 0;
        } else {
            dry_run++;
            if (st == 1) dry_late++; else if (st == 2) dry_busy++; else dry_nohost++;
        }
        for (int i = 0; i < B; i++) {
            if (!isfinite(outL[i]) || !isfinite(outR[i])) nan_out++;
            sink += outL[i] * outL[i];
        }
        uint64_t te = now_ns();
        late[nlate++] = te - tick;
        if (te - tick > P) overruns++;
    }
    if (dry_run > max_dry_run) max_dry_run = dry_run;
    double wall = (now_ns() - t_start) / 1e9;
    struct rusage ru1;
    getrusage(RUSAGE_THREAD, &ru1);
    double eng_cpu = (ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) + (ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) / 1e6 +
                     (ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) + (ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;
    double host_cpu_s = 0; long hv = 0, hn = 0;
    if (!inproc) {
        host_cpu_s = proc_cpu_s(host_pid, &hv, &hn);
        double dead = host_cpu_s_dead;
        if (ndeath == 0) host_cpu_s -= host_cpu0; else host_cpu_s += dead;   // approx. si relances
        stop_all = 1;
        kill(host_pid, SIGKILL);
        usleep(20000);
        shm_unlink(shm_name);
    }
    printf("{\n  \"mode\": \"%s\", \"uri\": \"%s\", \"params\": \"%s\", \"block\": %d, \"period_us\": %.1f,\n", mode, uri, params, B, P / 1000.0);
    printf("  \"engine_cpu\": %d, \"host_cpu\": %d, \"host_fifo\": %d, \"fifo\": %d, \"deadline_us\": %.0f, \"spin_us\": %.0f, \"hang_ms\": %d,\n",
           engine_cpu, host_cpu, host_prio >= 0 ? host_prio : prio, prio, deadline_us, spin_us, hang_ms);
    printf("  \"slow_us\": %ld, \"slow_every\": %ld, \"kill_every_s\": %.1f, \"async_offset_us\": %d,\n", slow_us, slow_every, kill_every_s, async_off_us);
    printf("  \"plugin_latency_samples\": %d, \"seconds\": %.1f, \"blocks\": %ld, \"ok\": %ld,\n", plat, wall, nlate, ok);
    printf("  \"dry_deadline\": %ld, \"dry_busy\": %ld, \"dry_nohost\": %ld, \"max_consecutive_dry\": %ld,\n", dry_late, dry_busy, dry_nohost, max_dry_run);
    printf("  \"engine_overruns\": %ld, \"nan_samples\": %ld, \"sink\": %.3f,\n", overruns, nan_out, isfinite(sink) ? sink / (nlate * B + 1) : -1.0);   // JSON : jamais « nan »
    printf("  \"engine_cpu_pct\": %.2f, \"host_cpu_pct\": %.2f, \"host_vol_ctxsw\": %ld, \"host_invol_ctxsw\": %ld,\n",
           100 * eng_cpu / wall, 100 * host_cpu_s / wall, hv - hv0, hn - hn0);
    printf("  \"engine_vol_ctxsw\": %ld, \"engine_invol_ctxsw\": %ld,\n", ru1.ru_nvcsw - ru0.ru_nvcsw, ru1.ru_nivcsw - ru0.ru_nivcsw);
    printf("  \"deaths\": %d, \"watchdog_kills\": %d, \"rt_watchdog_kills\": %d, \"chaos_kills\": %d, \"restart_ms\": [", ndeath, nkills_watchdog, nkills_rt, nkills_chaos);
    for (int i = 0; i < nrestart; i++) printf("%s%.1f", i ? ", " : "", restart_ns[i] / 1e6);
    printf("],\n  \"host_init_ms\": [");
    if (inproc) printf("%.1f", inproc_init / 1e6);
    for (int i = 0; i < ninit; i++) printf("%s%.1f", i ? ", " : "", init_ns[i] / 1e6);
    printf("],\n");
    stats("roundtrip", rt, nrt, 0);
    stats("plugin_process", proc, nproc, 0);
    stats("transport_overhead", ovh, novh, 0);
    stats("engine_wake_jitter", wake, N < nlate ? N : nlate, 0);
    stats("engine_block_end", late, nlate, 1);
    printf("}\n");
    return 0;
}
