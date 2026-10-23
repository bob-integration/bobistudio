/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * A REAL CLAP plugin file, built by tests/verif_console_plugins_clap_vst3.py into a temporary folder — the host's
 * CLAP backend (host/src/clap.rs) is tested on it offline. Declarations written here from the CLAP 1.2 headers
 * (MIT, Copyright (c) 2021 Alexandre BIQUE): only what these two plugins use.
 *
 *  org.bobistudio.test.delaygain   stereo; Gain [0, 2], Delay (stepped, samples, = DECLARED LATENCY, latency.changed
 *                                  on change), Mode (enum: Clean / Invert / Mute), Peak (read-only: highest |input|
 *                                  seen, INTERNAL data carried by clap.state), Bypass (flagged bypass: the console
 *                                  must not expose it);
 *  org.bobistudio.test.escape      pass-through; at its first process() it tries what a hostile plugin would
 *                                  (tests/data/escape_probes.h) and reports each verdict as a read-only parameter.
 */
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "escape_probes.h"

typedef struct { uint32_t major, minor, revision; } clap_version_t;
typedef struct clap_host {
    clap_version_t clap_version; void *host_data; const char *name, *vendor, *url, *version;
    const void *(*get_extension)(const struct clap_host *, const char *);
    void (*request_restart)(const struct clap_host *); void (*request_process)(const struct clap_host *);
    void (*request_callback)(const struct clap_host *);
} clap_host_t;
typedef struct {
    clap_version_t clap_version; const char *id, *name, *vendor, *url, *manual_url, *support_url, *version, *description;
    const char *const *features;
} clap_plugin_descriptor_t;
typedef struct { uint32_t size, time; uint16_t space_id, type; uint32_t flags; } clap_event_header_t;
typedef struct {
    clap_event_header_t header; uint32_t param_id; void *cookie; int32_t note_id; int16_t port_index, channel, key; double value;
} clap_event_param_value_t;
typedef struct clap_input_events {
    void *ctx; uint32_t (*size)(const struct clap_input_events *);
    const clap_event_header_t *(*get)(const struct clap_input_events *, uint32_t);
} clap_input_events_t;
typedef struct clap_output_events {
    void *ctx; bool (*try_push)(const struct clap_output_events *, const clap_event_header_t *);
} clap_output_events_t;
typedef struct { float **data32; double **data64; uint32_t channel_count, latency; uint64_t constant_mask; } clap_audio_buffer_t;
typedef struct {
    int64_t steady_time; uint32_t frames_count; const void *transport; const clap_audio_buffer_t *audio_inputs;
    clap_audio_buffer_t *audio_outputs; uint32_t audio_inputs_count, audio_outputs_count;
    const clap_input_events_t *in_events; const clap_output_events_t *out_events;
} clap_process_t;
typedef struct clap_plugin {
    const clap_plugin_descriptor_t *desc; void *plugin_data;
    bool (*init)(const struct clap_plugin *); void (*destroy)(const struct clap_plugin *);
    bool (*activate)(const struct clap_plugin *, double, uint32_t, uint32_t); void (*deactivate)(const struct clap_plugin *);
    bool (*start_processing)(const struct clap_plugin *); void (*stop_processing)(const struct clap_plugin *);
    void (*reset)(const struct clap_plugin *); int32_t (*process)(const struct clap_plugin *, const clap_process_t *);
    const void *(*get_extension)(const struct clap_plugin *, const char *); void (*on_main_thread)(const struct clap_plugin *);
} clap_plugin_t;
typedef struct {
    uint32_t id, flags; void *cookie; char name[256]; char module[1024]; double min_value, max_value, default_value;
} clap_param_info_t;
typedef struct {
    uint32_t (*count)(const clap_plugin_t *); bool (*get_info)(const clap_plugin_t *, uint32_t, clap_param_info_t *);
    bool (*get_value)(const clap_plugin_t *, uint32_t, double *);
    bool (*value_to_text)(const clap_plugin_t *, uint32_t, double, char *, uint32_t);
    bool (*text_to_value)(const clap_plugin_t *, uint32_t, const char *, double *);
    void (*flush)(const clap_plugin_t *, const clap_input_events_t *, const clap_output_events_t *);
} clap_plugin_params_t;
typedef struct { uint32_t id; char name[256]; uint32_t flags, channel_count; const char *port_type; uint32_t in_place_pair; } clap_audio_port_info_t;
typedef struct {
    uint32_t (*count)(const clap_plugin_t *, bool); bool (*get)(const clap_plugin_t *, uint32_t, bool, clap_audio_port_info_t *);
} clap_plugin_audio_ports_t;
typedef struct { uint32_t (*get)(const clap_plugin_t *); } clap_plugin_latency_t;
typedef struct { void (*changed)(const clap_host_t *); } clap_host_latency_t;
typedef struct clap_istream { void *ctx; int64_t (*read)(const struct clap_istream *, void *, uint64_t); } clap_istream_t;
typedef struct clap_ostream { void *ctx; int64_t (*write)(const struct clap_ostream *, const void *, uint64_t); } clap_ostream_t;
typedef struct {
    bool (*save)(const clap_plugin_t *, const clap_ostream_t *); bool (*load)(const clap_plugin_t *, const clap_istream_t *);
} clap_plugin_state_t;
typedef struct {
    uint32_t (*get_plugin_count)(const void *); const clap_plugin_descriptor_t *(*get_plugin_descriptor)(const void *, uint32_t);
    const clap_plugin_t *(*create_plugin)(const void *, const clap_host_t *, const char *);
} clap_plugin_factory_t;
typedef struct {
    clap_version_t clap_version; bool (*init)(const char *); void (*deinit)(void); const void *(*get_factory)(const char *);
} clap_plugin_entry_t;

enum { STEPPED = 1, HIDDEN = 4, READONLY = 8, BYPASS = 16, IS_ENUM = 1 << 16 };
enum { P_GAIN = 1, P_DELAY = 2, P_MODE = 3, P_PEAK = 4, P_BYPASS = 5 };
#define RING 4801
#define MAGIC 0x31544342u /* "BCT1" */

static const char *const F_DG[] = {"audio-effect", "delay", "stereo", NULL};
static const char *const F_ESC[] = {"audio-effect", "utility", NULL};
static const clap_plugin_descriptor_t D_DG = {{1, 2, 0}, "org.bobistudio.test.delaygain", "BobiTest Delay Gain", "Bobi tests",
                                              "", "", "", "1.0", "", F_DG};
static const clap_plugin_descriptor_t D_ESC = {{1, 2, 0}, "org.bobistudio.test.escape", "BobiTest Escape", "Bobi tests",
                                               "", "", "", "1.0", "", F_ESC};

typedef struct {
    clap_plugin_t p;
    const clap_host_t *host;
    int escape;
    double gain, delay, mode, peak, bypass;
    double esc[ESC_N];
    int probed;
    float *ring[2];
    uint32_t pos;
} T;

static T *me(const clap_plugin_t *p) { return (T *)p->plugin_data; }

static void latency_changed(T *t) {
    const clap_host_latency_t *l = t->host->get_extension(t->host, "clap.latency");
    if (l) l->changed(t->host);
}

static void set(T *t, uint32_t id, double v) {
    if (t->escape) return;
    switch (id) {
    case P_GAIN: t->gain = v; break;
    case P_DELAY: {
        double d = floor(v < 0 ? 0 : v > RING - 1 ? RING - 1 : v);
        if (d != t->delay) { t->delay = d; latency_changed(t); }
        break;
    }
    case P_MODE: t->mode = v; break;
    case P_BYPASS: t->bypass = v; break;
    }
}

static void events(T *t, const clap_input_events_t *in) {
    uint32_t n = in ? in->size(in) : 0;
    for (uint32_t i = 0; i < n; i++) {
        const clap_event_header_t *h = in->get(in, i);
        if (h && h->space_id == 0 && h->type == 5) {
            const clap_event_param_value_t *e = (const void *)h;
            set(t, e->param_id, e->value);
        }
    }
}

static bool p_init(const clap_plugin_t *p) { (void)p; return true; }
static void p_destroy(const clap_plugin_t *p) {
    T *t = me(p);
    free(t->ring[0]); free(t->ring[1]); free(t);
}
static bool p_activate(const clap_plugin_t *p, double sr, uint32_t a, uint32_t b) {
    (void)sr; (void)a; (void)b;
    T *t = me(p);
    memset(t->ring[0], 0, RING * sizeof(float)); memset(t->ring[1], 0, RING * sizeof(float)); t->pos = 0;
    return true;
}
static void p_deactivate(const clap_plugin_t *p) { (void)p; }
static bool p_start(const clap_plugin_t *p) { (void)p; return true; }
static void p_stop(const clap_plugin_t *p) { (void)p; }
static void p_reset(const clap_plugin_t *p) { (void)p; }

static int32_t p_process(const clap_plugin_t *p, const clap_process_t *pr) {
    T *t = me(p);
    events(t, pr->in_events);
    uint32_t n = pr->frames_count;
    if (pr->audio_inputs_count < 1 || pr->audio_outputs_count < 1) return 0;
    float **in = pr->audio_inputs[0].data32, **out = pr->audio_outputs[0].data32;
    if (t->escape) {
        if (!t->probed) { t->probed = 1; escape_probes(t->esc); }
        for (int c = 0; c < 2; c++) memcpy(out[c], in[c], n * sizeof(float));
        return 1;
    }
    float g = (float)t->gain * (t->mode == 1 ? -1.0f : t->mode == 2 ? 0.0f : 1.0f);
    uint32_t d = (uint32_t)t->delay;
    double pk = t->peak;
    for (uint32_t i = 0; i < n; i++) {
        for (int c = 0; c < 2; c++) {
            float x = in[c][i];
            if (fabsf(x) > pk) pk = fabsf(x);
            t->ring[c][t->pos] = x;
            out[c][i] = g * t->ring[c][(t->pos + RING - d) % RING];
        }
        t->pos = (t->pos + 1) % RING;
    }
    if (pk != t->peak) {
        t->peak = pk;
        clap_event_param_value_t e = {{sizeof e, 0, 0, 5, 0}, P_PEAK, NULL, -1, -1, -1, -1, pk};
        pr->out_events->try_push(pr->out_events, &e.header);
    }
    return 1;
}

/* params */
static uint32_t pa_count(const clap_plugin_t *p) { return me(p)->escape ? ESC_N : 5; }
static bool pa_info(const clap_plugin_t *p, uint32_t i, clap_param_info_t *o) {
    memset(o, 0, sizeof *o);
    if (me(p)->escape) {
        if (i >= ESC_N) return false;
        o->id = 100 + i; o->flags = READONLY | STEPPED; o->max_value = 1;
        snprintf(o->name, sizeof o->name, "%s", ESC_NAMES[i]);
        return true;
    }
    switch (i) {
    case 0: o->id = P_GAIN; o->max_value = 2; o->default_value = 1; strcpy(o->name, "Gain"); strcpy(o->module, "Level"); break;
    case 1: o->id = P_DELAY; o->flags = STEPPED; o->max_value = RING - 1; strcpy(o->name, "Delay"); strcpy(o->module, "Time"); break;
    case 2: o->id = P_MODE; o->flags = STEPPED | IS_ENUM; o->max_value = 2; strcpy(o->name, "Mode"); break;
    case 3: o->id = P_PEAK; o->flags = READONLY; o->max_value = 10; strcpy(o->name, "Peak"); break;
    case 4: o->id = P_BYPASS; o->flags = STEPPED | BYPASS; o->max_value = 1; strcpy(o->name, "Bypass"); break;
    default: return false;
    }
    return true;
}
static bool pa_value(const clap_plugin_t *p, uint32_t id, double *v) {
    T *t = me(p);
    if (t->escape) {
        if (id < 100 || id >= 100 + ESC_N) return false;
        *v = t->probed ? t->esc[id - 100] : 0;
        return true;
    }
    switch (id) {
    case P_GAIN: *v = t->gain; return true;
    case P_DELAY: *v = t->delay; return true;
    case P_MODE: *v = t->mode; return true;
    case P_PEAK: *v = t->peak; return true;
    case P_BYPASS: *v = t->bypass; return true;
    }
    return false;
}
static bool pa_text(const clap_plugin_t *p, uint32_t id, double v, char *b, uint32_t n) {
    (void)p;
    static const char *const modes[] = {"Clean", "Invert", "Mute"};
    if (id == P_MODE && v >= 0 && v <= 2) snprintf(b, n, "%s", modes[(int)(v + 0.5)]);
    else if (id == P_GAIN) snprintf(b, n, "%.2f x", v);
    else if (id == P_DELAY) snprintf(b, n, "%.0f smp", v);
    else snprintf(b, n, "%g", v);
    return true;
}
static bool pa_from_text(const clap_plugin_t *p, uint32_t id, const char *s, double *v) {
    (void)p; (void)id; *v = atof(s); return true;
}
static void pa_flush(const clap_plugin_t *p, const clap_input_events_t *in, const clap_output_events_t *out) {
    (void)out; events(me(p), in);
}
static const clap_plugin_params_t PARAMS = {pa_count, pa_info, pa_value, pa_text, pa_from_text, pa_flush};

static uint32_t ap_count(const clap_plugin_t *p, bool in) { (void)p; (void)in; return 1; }
static bool ap_get(const clap_plugin_t *p, uint32_t i, bool in, clap_audio_port_info_t *o) {
    (void)p; (void)in;
    if (i) return false;
    memset(o, 0, sizeof *o); o->id = 0; strcpy(o->name, "main"); o->flags = 1; o->channel_count = 2; o->port_type = "stereo";
    o->in_place_pair = 0xFFFFFFFFu;
    return true;
}
static const clap_plugin_audio_ports_t PORTS = {ap_count, ap_get};
static uint32_t lat_get(const clap_plugin_t *p) { return (uint32_t)me(p)->delay; }
static const clap_plugin_latency_t LATENCY = {lat_get};

static bool st_save(const clap_plugin_t *p, const clap_ostream_t *s) {
    T *t = me(p);
    double v[5] = {MAGIC, t->gain, t->delay, t->mode, t->peak};
    return s->write(s, v, sizeof v) == (int64_t)sizeof v;
}
static bool st_load(const clap_plugin_t *p, const clap_istream_t *s) {
    T *t = me(p);
    double v[5];
    if (s->read(s, v, sizeof v) != (int64_t)sizeof v || v[0] != MAGIC) return false;
    t->gain = v[1]; set(t, P_DELAY, v[2]); t->mode = v[3]; t->peak = v[4];
    return true;
}
static const clap_plugin_state_t STATE = {st_save, st_load};

static const void *p_ext(const clap_plugin_t *p, const char *id) {
    if (!strcmp(id, "clap.params")) return &PARAMS;
    if (!strcmp(id, "clap.audio-ports")) return &PORTS;
    if (me(p)->escape) return NULL;
    if (!strcmp(id, "clap.latency")) return &LATENCY;
    if (!strcmp(id, "clap.state")) return &STATE;
    return NULL;
}
static void p_main(const clap_plugin_t *p) { (void)p; }

static uint32_t f_count(const void *f) { (void)f; return 2; }
static const clap_plugin_descriptor_t *f_desc(const void *f, uint32_t i) { (void)f; return i == 0 ? &D_DG : i == 1 ? &D_ESC : NULL; }
static const clap_plugin_t *f_create(const void *f, const clap_host_t *h, const char *id) {
    (void)f;
    int esc = !strcmp(id, D_ESC.id);
    if (!esc && strcmp(id, D_DG.id)) return NULL;
    T *t = calloc(1, sizeof *t);
    t->host = h; t->escape = esc; t->gain = 1;
    t->ring[0] = calloc(RING, sizeof(float)); t->ring[1] = calloc(RING, sizeof(float));
    t->p = (clap_plugin_t){esc ? &D_ESC : &D_DG, t, p_init, p_destroy, p_activate, p_deactivate, p_start, p_stop, p_reset,
                           p_process, p_ext, p_main};
    return &t->p;
}
static const clap_plugin_factory_t FACTORY = {f_count, f_desc, f_create};
static bool e_init(const char *path) { (void)path; return true; }
static void e_deinit(void) {}
static const void *e_factory(const char *id) { return !strcmp(id, "clap.plugin-factory") ? &FACTORY : NULL; }
__attribute__((visibility("default"))) const clap_plugin_entry_t clap_entry = {{1, 2, 0}, e_init, e_deinit, e_factory};
