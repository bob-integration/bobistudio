/* SPDX-License-Identifier: GPL-3.0-or-later
 * Minimal LV2 plugin WITH the state interface, compiled by tests/verif_console_plugins_lv2_state.py: no LV2
 * header needed (the few ABI structures are declared here). Mono pass-through; while `learn` is on it keeps the
 * peak of its input — INTERNAL data, not a control port — and reports it on the `peak` output port. Its state is
 * that peak (atom:Float, plain data). With `nonpod` on, save() also stores a property WITHOUT the POD flag, which
 * the console's host must refuse (its state has to be storable in a document). */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct { const char *URI; void *data; } LV2_Feature;
typedef struct { void *handle; uint32_t (*map)(void *, const char *); } LV2_URID_Map;
typedef struct LV2_Descriptor {
    const char *URI;
    void *(*instantiate)(const struct LV2_Descriptor *, double, const char *, const LV2_Feature *const *);
    void (*connect_port)(void *, uint32_t, void *);
    void (*activate)(void *);
    void (*run)(void *, uint32_t);
    void (*deactivate)(void *);
    void (*cleanup)(void *);
    const void *(*extension_data)(const char *);
} LV2_Descriptor;
typedef uint32_t (*Store)(void *, uint32_t, const void *, size_t, uint32_t, uint32_t);
typedef const void *(*Retrieve)(void *, uint32_t, size_t *, uint32_t *, uint32_t *);
typedef struct {
    uint32_t (*save)(void *, Store, void *, uint32_t, const LV2_Feature *const *);
    uint32_t (*restore)(void *, Retrieve, void *, uint32_t, const LV2_Feature *const *);
} LV2_State_Interface;

#define URI_PLUGIN "urn:bobistudio:lv2test:state"
#define POD 1u
#define PORTABLE 2u

typedef struct {
    const float *in; float *out; const float *learn; const float *nonpod; float *peak_port;
    float peak;
    uint32_t k_peak, k_junk, t_float;
} S;

static void *instantiate(const LV2_Descriptor *d, double rate, const char *b, const LV2_Feature *const *f) {
    (void)d; (void)rate; (void)b;
    LV2_URID_Map *map = NULL;
    for (int i = 0; f && f[i]; i++)
        if (!strcmp(f[i]->URI, "http://lv2plug.in/ns/ext/urid#map")) map = f[i]->data;
    if (!map) return NULL;
    S *s = calloc(1, sizeof(S));
    s->k_peak = map->map(map->handle, "urn:bobistudio:lv2test:state#peak");
    s->k_junk = map->map(map->handle, "urn:bobistudio:lv2test:state#junk");
    s->t_float = map->map(map->handle, "http://lv2plug.in/ns/ext/atom#Float");
    return s;
}
static void connect_port(void *h, uint32_t p, void *d) {
    S *s = h;
    switch (p) {
    case 0: s->in = d; break;
    case 1: s->out = d; break;
    case 2: s->learn = d; break;
    case 3: s->nonpod = d; break;
    case 4: s->peak_port = d; break;
    }
}
static void run(void *h, uint32_t n) {
    S *s = h;
    for (uint32_t i = 0; i < n; i++) {
        s->out[i] = s->in[i];
        if (*s->learn > 0.5f && fabsf(s->in[i]) > s->peak) s->peak = fabsf(s->in[i]);
    }
    *s->peak_port = s->peak;
}
static void cleanup(void *h) { free(h); }
static uint32_t save(void *h, Store store, void *sh, uint32_t flags, const LV2_Feature *const *f) {
    (void)flags; (void)f;
    S *s = h;
    uint32_t st = store(sh, s->k_peak, &s->peak, sizeof(float), s->t_float, POD | PORTABLE);
    if (st == 0 && *s->nonpod > 0.5f) st = store(sh, s->k_junk, &s->peak, sizeof(float), s->t_float, 0);
    return st;
}
static uint32_t restore(void *h, Retrieve get, void *gh, uint32_t flags, const LV2_Feature *const *f) {
    (void)flags; (void)f;
    S *s = h;
    size_t size = 0; uint32_t type = 0, fl = 0;
    const float *v = get(gh, s->k_peak, &size, &type, &fl);
    if (v && size == sizeof(float) && type == s->t_float) s->peak = *v;
    return 0;
}
static const LV2_State_Interface state_iface = {save, restore};
static const void *extension_data(const char *uri) {
    return strcmp(uri, "http://lv2plug.in/ns/ext/state#interface") ? NULL : &state_iface;
}
static const LV2_Descriptor desc = {URI_PLUGIN, instantiate, connect_port, NULL, run, NULL, cleanup, extension_data};
__attribute__((visibility("default"))) const LV2_Descriptor *lv2_descriptor(uint32_t i) { return i ? NULL : &desc; }
