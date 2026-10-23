// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// plugcore.c — hôte LV2 MINIMAL (lilv), compilé en bibliothèque partagée `libplugcore.so`.
// Banc « plugins tiers, étape 0 » (docs/chantiers/CONSOLE_PLUGINS_TIERS_BANC0.md).
//
// Le MÊME code de traitement sert aux deux modes du banc, pour que la comparaison ne mesure que le
// transport : IN-PROCESS (le « moteur » dlopen() cette bibliothèque et appelle pc_process) et
// CO-PROCESSUS (l'exécutable `host` la lie et l'appelle sur réveil).
//
// URI spéciale "null" : copie entrée→sortie sans lilv (mesure du transport seul).
// Injection de fautes (pc_set_fault) : retard artificiel tous les N blocs (sommeil si slow_us > 0,
// boucle active si slow_us < 0), plantage au bloc K.
//
// Ce n'est PAS un hôte de production : pas d'état, pas de presets, worker exécuté en ligne
// (synchrone, donc NON temps réel), paramètres aux valeurs par défaut sauf `sym=val` explicites.

#define _GNU_SOURCE
#include <lilv/lilv.h>
#include <lv2/atom/atom.h>
#include <lv2/buf-size/buf-size.h>
#include <lv2/core/lv2.h>
#include <lv2/options/options.h>
#include <lv2/urid/urid.h>
#include <lv2/worker/worker.h>
#include <lv2/state/state.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAXB 4096
#define MAXP 512
#define MAXURID 1024
#define ATOM_CAP 8192

static LilvWorld *world;
static const LilvPlugin *plug;
static LilvInstance *inst;
static int nullmode, B, latency_port = -1, nports;
static float ctl[MAXP];
static float dummy_out[MAXB], zero_in[MAXB];
static void *atom_buf[MAXP];
static int atom_is_out[MAXP];
static int audio_in[8], audio_out[8], n_ain, n_aout;
static char last_err[512];
static uint32_t urid_chunk;

static char *urids[MAXURID];
static int nurid;
static LV2_URID map_uri(LV2_URID_Map_Handle h, const char *uri) {
    (void)h;
    for (int i = 0; i < nurid; i++) if (!strcmp(urids[i], uri)) return i + 1;
    if (nurid >= MAXURID) return 0;
    urids[nurid] = strdup(uri);
    return ++nurid;
}
static const char *unmap_uri(LV2_URID_Unmap_Handle h, LV2_URID u) {
    (void)h;
    return (u >= 1 && (int)u <= nurid) ? urids[u - 1] : NULL;
}
static LV2_URID_Map map = {NULL, map_uri};
static LV2_URID_Unmap unmap = {NULL, unmap_uri};

// Worker en ligne : work() appelé dans schedule_work, réponses rejouées après run().
static const LV2_Worker_Interface *worker_iface;
static uint8_t resp_buf[ATOM_CAP];
static uint32_t resp_size;
static LV2_Worker_Status respond(LV2_Worker_Respond_Handle h, uint32_t size, const void *data) {
    (void)h;
    if (size > sizeof resp_buf) return LV2_WORKER_ERR_NO_SPACE;
    memcpy(resp_buf, data, size);
    resp_size = size;
    return LV2_WORKER_SUCCESS;
}
static LV2_Worker_Status schedule_work(LV2_Worker_Schedule_Handle h, uint32_t size, const void *data) {
    (void)h;
    if (!worker_iface) return LV2_WORKER_ERR_UNKNOWN;
    return worker_iface->work(lilv_instance_get_handle(inst), respond, NULL, size, data);
}
static LV2_Worker_Schedule sched = {NULL, schedule_work};

static int32_t opt_min = 1, opt_max, opt_nom;
static float opt_rate;
static LV2_Options_Option options[5];
static LV2_Feature f_map = {LV2_URID__map, &map}, f_unmap = {LV2_URID__unmap, &unmap},
                   f_sched = {LV2_WORKER__schedule, &sched}, f_opts = {LV2_OPTIONS__options, options},
                   f_bounded = {LV2_BUF_SIZE__boundedBlockLength, NULL};
static const LV2_Feature *features[] = {&f_map, &f_unmap, &f_sched, &f_opts, &f_bounded, NULL};

// Fautes injectées (banc) : retard de slow_us tous les slow_every blocs ; plantage au bloc crash_at.
static long fault_slow_us, fault_slow_every, fault_crash_at, blockno;
void pc_set_fault(long slow_us, long slow_every, long crash_at) {
    fault_slow_us = slow_us; fault_slow_every = slow_every; fault_crash_at = crash_at;
}

const char *pc_error(void) { return last_err; }
int pc_latency(void) { return latency_port >= 0 ? (int)ctl[latency_port] : 0; }
int pc_audio_ports(void) { return n_ain * 10 + n_aout; }

static void set_param(const char *kv) {
    char sym[128]; float v;
    if (sscanf(kv, "%127[^=]=%f", sym, &v) != 2) return;
    LilvNode *s = lilv_new_string(world, sym);
    const LilvPort *p = lilv_plugin_get_port_by_symbol(plug, s);
    lilv_node_free(s);
    if (p) ctl[lilv_port_get_index(plug, p)] = v;
}

int pc_open(const char *uri, double fs, int block, const char *params) {
    B = block;
    if (B > MAXB) { snprintf(last_err, sizeof last_err, "block too big"); return -1; }
    if (!strcmp(uri, "null")) { nullmode = 1; return 0; }
    opt_max = opt_nom = B; opt_rate = (float)fs;
    world = lilv_world_new();
    lilv_world_load_all(world);       // LV2_PATH restreint au bundle par l'appelant (temps de relance)
    LilvNode *u = lilv_new_uri(world, uri);
    plug = lilv_plugins_get_by_uri(lilv_world_get_all_plugins(world), u);
    lilv_node_free(u);
    if (!plug) { snprintf(last_err, sizeof last_err, "plugin not found: %s", uri); return -1; }
    LV2_URID tInt = map_uri(NULL, LV2_ATOM__Int), tFloat = map_uri(NULL, LV2_ATOM__Float);
    options[0] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(NULL, LV2_BUF_SIZE__minBlockLength), 4, tInt, &opt_min};
    options[1] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(NULL, LV2_BUF_SIZE__maxBlockLength), 4, tInt, &opt_max};
    options[2] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(NULL, LV2_BUF_SIZE__nominalBlockLength), 4, tInt, &opt_nom};
    options[3] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(NULL, "http://lv2plug.in/ns/ext/parameters#sampleRate"), 4, tFloat, &opt_rate};
    options[4] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, 0, 0, 0, NULL};
    // Fonctions requises que nous ne fournissons pas → échec explicite (jamais un chargement bancal).
    LilvNodes *req = lilv_plugin_get_required_features(plug);
    LILV_FOREACH(nodes, i, req) {
        const char *f = lilv_node_as_uri(lilv_nodes_get(req, i));
        int ok = 0;
        for (int k = 0; features[k]; k++) if (!strcmp(features[k]->URI, f)) ok = 1;
        if (!ok && strcmp(f, "http://lv2plug.in/ns/lv2core#isLive") && strcmp(f, "http://lv2plug.in/ns/lv2core#inPlaceBroken")) {
            snprintf(last_err, sizeof last_err, "missing feature %s", f);
            lilv_nodes_free(req);
            return -1;
        }
    }
    lilv_nodes_free(req);
    inst = lilv_plugin_instantiate(plug, fs, features);
    if (!inst) { snprintf(last_err, sizeof last_err, "instantiate failed"); return -1; }
    worker_iface = lilv_instance_get_extension_data(inst, LV2_WORKER__interface);
    nports = lilv_plugin_get_num_ports(plug);
    if (nports > MAXP) { snprintf(last_err, sizeof last_err, "too many ports"); return -1; }
    float mins[MAXP], maxs[MAXP], defs[MAXP];
    lilv_plugin_get_port_ranges_float(plug, mins, maxs, defs);
    LilvNode *cAudio = lilv_new_uri(world, LV2_CORE__AudioPort), *cCtl = lilv_new_uri(world, LV2_CORE__ControlPort),
             *cIn = lilv_new_uri(world, LV2_CORE__InputPort), *cAtom = lilv_new_uri(world, LV2_ATOM__AtomPort),
             *cCV = lilv_new_uri(world, LV2_CORE__CVPort), *rLat = lilv_new_uri(world, LV2_CORE__reportsLatency),
             *dLat = lilv_new_uri(world, LV2_CORE__latency),
             *pDes = lilv_new_uri(world, "http://lv2plug.in/ns/lv2core#designation");
    urid_chunk = map_uri(NULL, LV2_ATOM__Chunk);
    for (int i = 0; i < nports; i++) {
        const LilvPort *p = lilv_plugin_get_port_by_index(plug, i);
        int in = lilv_port_is_a(plug, p, cIn);
        if (lilv_port_is_a(plug, p, cAudio)) {
            if (in && n_ain < 8) audio_in[n_ain++] = i;
            else if (!in && n_aout < 8) audio_out[n_aout++] = i;
            lilv_instance_connect_port(inst, i, in ? zero_in : dummy_out);
        } else if (lilv_port_is_a(plug, p, cCtl)) {
            ctl[i] = isnan(defs[i]) ? (isnan(mins[i]) ? 0.f : mins[i]) : defs[i];
            if (!in && lilv_port_has_property(plug, p, rLat)) latency_port = i;
            LilvNode *des = lilv_port_get(plug, p, pDes);
            if (des && !in && lilv_node_equals(des, dLat)) latency_port = i;
            if (des) lilv_node_free(des);
            lilv_instance_connect_port(inst, i, &ctl[i]);
        } else if (lilv_port_is_a(plug, p, cAtom)) {
            atom_buf[i] = calloc(1, ATOM_CAP);
            atom_is_out[i] = !in;
            LV2_Atom_Sequence *s = atom_buf[i];
            s->atom.type = map_uri(NULL, LV2_ATOM__Sequence);
            s->atom.size = sizeof(LV2_Atom_Sequence_Body);
            lilv_instance_connect_port(inst, i, s);
        } else if (lilv_port_is_a(plug, p, cCV)) {
            lilv_instance_connect_port(inst, i, in ? zero_in : dummy_out);
        } else {
            lilv_instance_connect_port(inst, i, NULL);
        }
    }
    if (params && *params) {
        char *dup = strdup(params), *sv, *t;
        for (t = strtok_r(dup, ",", &sv); t; t = strtok_r(NULL, ",", &sv)) set_param(t);
        free(dup);
    }
    if (n_ain < 1 || n_aout < 1) { snprintf(last_err, sizeof last_err, "no audio in/out"); return -1; }
    lilv_instance_activate(inst);
    return 0;
}

static void sleep_us(long us) {
    struct timespec ts = {us / 1000000, (us % 1000000) * 1000};
    nanosleep(&ts, NULL);
}

// Traite un bloc stéréo. Plugin mono (1 entrée, 1 sortie) : canal gauche traité, droit recopié.
void pc_process(const float *inL, const float *inR, float *outL, float *outR, int n) {
    blockno++;
    if (fault_crash_at && blockno == fault_crash_at) raise(SIGSEGV);
    if (fault_slow_every && blockno % fault_slow_every == 0) {
        if (fault_slow_us >= 0) sleep_us(fault_slow_us);
        else {                                // < 0 : boucle ACTIVE de -slow_us µs (plugin qui calcule trop)
            struct timespec a, b;
            clock_gettime(CLOCK_MONOTONIC, &a);
            do clock_gettime(CLOCK_MONOTONIC, &b);
            while ((b.tv_sec - a.tv_sec) * 1000000L + (b.tv_nsec - a.tv_nsec) / 1000 < -fault_slow_us);
        }
    }
    if (nullmode) { memcpy(outL, inL, n * 4); memcpy(outR, inR, n * 4); return; }
    lilv_instance_connect_port(inst, audio_in[0], (void *)inL);
    lilv_instance_connect_port(inst, audio_out[0], outL);
    if (n_ain > 1) lilv_instance_connect_port(inst, audio_in[1], (void *)inR);
    if (n_aout > 1) lilv_instance_connect_port(inst, audio_out[1], outR);
    for (int i = 0; i < nports; i++)
        if (atom_buf[i] && atom_is_out[i]) {
            LV2_Atom_Sequence *s = atom_buf[i];
            s->atom.type = urid_chunk;
            s->atom.size = ATOM_CAP - sizeof(LV2_Atom);
        }
    lilv_instance_run(inst, n);
    if (worker_iface && resp_size) {
        worker_iface->work_response(lilv_instance_get_handle(inst), resp_size, resp_buf);
        resp_size = 0;
    }
    if (worker_iface && worker_iface->end_run) worker_iface->end_run(lilv_instance_get_handle(inst));
    if (n_aout < 2) memcpy(outR, inR, n * 4);
}

void pc_close(void) {
    if (inst) { lilv_instance_deactivate(inst); lilv_instance_free(inst); inst = NULL; }
    if (world) { lilv_world_free(world); world = NULL; }
}
