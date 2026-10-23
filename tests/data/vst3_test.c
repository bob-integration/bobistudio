/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * A REAL VST3 plugin (Linux binary layout, written in C), built by tests/verif_console_plugins_clap_vst3.py into a
 * temporary bundle `bobi_test.vst3/Contents/x86_64-linux/bobi_test.so` — the host's VST3 backend (host/src/vst3.rs)
 * is tested on it offline. Interface ids and table orders follow Steinberg's pluginterfaces (MIT since VST SDK
 * 3.8.0, Copyright (c) 2025 Steinberg Media Technologies GmbH); only what these classes use is declared.
 * One object answers IComponent, IAudioProcessor and IEditController (single-component plugin).
 *
 *  class 1 « BobiTest VST3 Delay Gain »: Gain (plain = 2 x normalized), Delay (4800 steps, samples, = DECLARED
 *           LATENCY, restartComponent(kLatencyChanged) when it changes), Mode (list: Clean / Invert / Mute), Peak
 *           (read-only, reported through outputParameterChanges, kept in the state), Bypass (kIsBypass: hidden);
 *           Two habits of real plugins (Surge XT Effects, JUCE — bench 2026-10-18) the host must survive: a Gain
 *           received in process() is REPORTED BACK with its OLD value in outputParameterChanges (a stale echo: a host
 *           that adopts it, or tells the controller, puts the old gain back); and setting Mode through the controller
 *           to the value it already has resets Gain to its default (a selector that resets the parameters it owns).
 *  class 2 « BobiTest VST3 Escape »: pass-through, tries what a hostile plugin would at its first process().
 */
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "escape_probes.h"

typedef int32_t tresult;
typedef uint8_t TUID[16];
#define K_OK 0
#define K_FALSE 1
#define K_NOINTF (-1)
#define B4(x) (uint8_t)((x) >> 24), (uint8_t)((x) >> 16), (uint8_t)((x) >> 8), (uint8_t)(x)
#define UID(a, b, c, d) {B4(a), B4(b), B4(c), B4(d)}
static const TUID I_FUNKNOWN = UID(0x00000000, 0x00000000, 0xC0000000, 0x00000046);
static const TUID I_FACTORY = UID(0x7A4D811C, 0x52114A1F, 0xAED9D2EE, 0x0B43BF9F);
static const TUID I_PLUGINBASE = UID(0x22888DDB, 0x156E45AE, 0x8358B348, 0x08190625);
static const TUID I_COMPONENT = UID(0xE831FF31, 0xF2D54301, 0x928EBBEE, 0x25697802);
static const TUID I_PROCESSOR = UID(0x42043F99, 0xB7DA453C, 0xA569E79D, 0x9AAEC33D);
static const TUID I_CONTROLLER = UID(0xDCD7BBE3, 0x7742448D, 0xA874AACC, 0x979C759E);
static const TUID C_DG = UID(0xB0B10001, 0x7E570001, 0x00000000, 0x0000DE1A);
static const TUID C_ESC = UID(0xB0B10001, 0x7E570002, 0x00000000, 0x0000E5CA);

typedef struct { tresult (*qi)(void *, const uint8_t *, void **); uint32_t (*add)(void *); uint32_t (*rel)(void *); } Unk;
typedef struct { Unk u; tresult (*read)(void *, void *, int32_t, int32_t *); tresult (*write)(void *, const void *, int32_t, int32_t *); } StreamV;
typedef struct { Unk u; int32_t (*count)(void *); void *(*data)(void *, int32_t); void *(*add)(void *, const uint32_t *, int32_t *); } ChangesV;
typedef struct { Unk u; uint32_t (*id)(void *); int32_t (*count)(void *); tresult (*get)(void *, int32_t, int32_t *, double *); tresult (*add)(void *, int32_t, double, int32_t *); } QueueV;
typedef struct { Unk u; tresult (*begin)(void *, uint32_t); tresult (*perform)(void *, uint32_t, double); tresult (*end)(void *, uint32_t); tresult (*restart)(void *, int32_t); } HandlerV;
typedef struct { int32_t num_channels; uint64_t silence; float **buf; } Bus;
typedef struct {
    int32_t mode, size, n, nin, nout; Bus *in, *out; void *ichg, *ochg, *iev, *oev, *ctx;
} PData;
typedef struct { int32_t mode, size, maxb; double rate; } PSetup;
typedef struct { int32_t media, dir, ch; uint16_t name[128]; int32_t type; uint32_t flags; } BusInfo;
typedef struct {
    uint32_t id; uint16_t title[128], short_title[128], units[128]; int32_t steps; double def; int32_t unit; int32_t flags;
} PInfo;
typedef struct { uint8_t cid[16]; int32_t card; char cat[32]; char name[64]; } ClassInfo;
typedef struct { char vendor[64]; char url[256]; char email[128]; int32_t flags; } FactInfo;

#define RING 4801
#define NP 5
enum { P_GAIN = 1, P_DELAY = 2, P_MODE = 3, P_PEAK = 4, P_BYPASS = 5 };

typedef struct Obj {
    const void *vc, *vp, *ve; /* IComponent, IAudioProcessor, IEditController */
    uint32_t refs;
    int esc;
    double norm[NP + 1];       /* normalized values by id */
    double esc_v[ESC_N];
    int probed;
    void *handler;
    float *ring[2];
    uint32_t pos;
    double last_delay;
    double echo_gain;          /* stale Gain to report in the next outputParameterChanges, < 0: none */
    double pgain;              /* the last Gain the PROCESSOR received in process() (the controller may know a newer) */
} Obj;

#define FROM_C(t) ((Obj *)(t))
#define FROM_P(t) ((Obj *)((char *)(t) - offsetof(Obj, vp)))
#define FROM_E(t) ((Obj *)((char *)(t) - offsetof(Obj, ve)))

static int same(const uint8_t *a, const TUID b) { return a && !memcmp(a, b, 16); }

static tresult o_qi(Obj *o, const uint8_t *iid, void **out) {
    if (same(iid, I_FUNKNOWN) || same(iid, I_PLUGINBASE) || same(iid, I_COMPONENT)) *out = &o->vc;
    else if (same(iid, I_PROCESSOR)) *out = &o->vp;
    else if (same(iid, I_CONTROLLER)) *out = &o->ve;
    else { *out = NULL; return K_NOINTF; }
    o->refs++;
    return K_OK;
}
static uint32_t o_rel(Obj *o) {
    if (--o->refs == 0) { free(o->ring[0]); free(o->ring[1]); free(o); return 0; }
    return o->refs;
}
static double plain(int id, double n) {
    switch (id) { case P_GAIN: return 2 * n; case P_DELAY: return round(n * (RING - 1)); case P_MODE: return round(2 * n);
                  case P_PEAK: return 10 * n; default: return n; }
}
static double normv(int id, double p) {
    switch (id) { case P_GAIN: return p / 2; case P_DELAY: return p / (RING - 1); case P_MODE: return p / 2;
                  case P_PEAK: return p / 10; default: return p; }
}
static void u16(uint16_t *d, const char *s) { int i = 0; for (; s[i] && i < 127; i++) d[i] = (uint8_t)s[i]; d[i] = 0; }

/* --- IComponent --- */
static tresult c_qi(void *t, const uint8_t *i, void **o) { return o_qi(FROM_C(t), i, o); }
static uint32_t c_add(void *t) { return ++FROM_C(t)->refs; }
static uint32_t c_rel(void *t) { return o_rel(FROM_C(t)); }
static tresult c_init(void *t, void *ctx) { (void)t; (void)ctx; return K_OK; }
static tresult c_term(void *t) { (void)t; return K_OK; }
static tresult c_ctrl_cid(void *t, uint8_t *cid) { (void)t; memset(cid, 0, 16); return K_FALSE; }
static tresult c_iomode(void *t, int32_t m) { (void)t; (void)m; return K_OK; }
static int32_t c_buscount(void *t, int32_t type, int32_t dir) { (void)t; (void)dir; return type == 0 ? 1 : 0; }
static tresult c_businfo(void *t, int32_t type, int32_t dir, int32_t i, BusInfo *b) {
    (void)t;
    if (type != 0 || i != 0) return K_FALSE;
    memset(b, 0, sizeof *b); b->media = 0; b->dir = dir; b->ch = 2; u16(b->name, "main"); b->type = 0; b->flags = 1;
    return K_OK;
}
static tresult c_routing(void *t, void *a, void *b) { (void)t; (void)a; (void)b; return K_FALSE; }
static tresult c_actbus(void *t, int32_t a, int32_t b, int32_t c, uint8_t s) { (void)t; (void)a; (void)b; (void)c; (void)s; return K_OK; }
static tresult c_active(void *t, uint8_t s) {
    Obj *o = FROM_C(t);
    if (s) { memset(o->ring[0], 0, RING * 4); memset(o->ring[1], 0, RING * 4); o->pos = 0; }
    return K_OK;
}
static tresult c_setstate(void *t, void *s) {
    Obj *o = FROM_C(t);
    double v[NP + 1];
    int32_t got = 0;
    if (((StreamV **)s)[0]->read(s, v, sizeof v, &got) != K_OK || got != (int32_t)sizeof v || v[0] != 4242.0) return K_FALSE;
    for (int i = 1; i <= NP; i++) o->norm[i] = v[i];
    return K_OK;
}
static tresult c_getstate(void *t, void *s) {
    Obj *o = FROM_C(t);
    double v[NP + 1];
    v[0] = 4242.0;
    for (int i = 1; i <= NP; i++) v[i] = o->norm[i];
    int32_t put = 0;
    return ((StreamV **)s)[0]->write(s, v, sizeof v, &put) == K_OK && put == (int32_t)sizeof v ? K_OK : K_FALSE;
}
static const struct { Unk u; void *f[11]; } VC = {{c_qi, c_add, c_rel}, {(void *)c_init, (void *)c_term, (void *)c_ctrl_cid,
    (void *)c_iomode, (void *)c_buscount, (void *)c_businfo, (void *)c_routing, (void *)c_actbus, (void *)c_active,
    (void *)c_setstate, (void *)c_getstate}};

/* --- IAudioProcessor --- */
static tresult p_qi(void *t, const uint8_t *i, void **o) { return o_qi(FROM_P(t), i, o); }
static uint32_t p_add(void *t) { return ++FROM_P(t)->refs; }
static uint32_t p_rel(void *t) { return o_rel(FROM_P(t)); }
static tresult p_arr(void *t, uint64_t *i, int32_t ni, uint64_t *o, int32_t no) {
    (void)t;
    return ni == 1 && no == 1 && i[0] == 3 && o[0] == 3 ? K_OK : K_FALSE;
}
static tresult p_getarr(void *t, int32_t d, int32_t i, uint64_t *a) { (void)t; (void)d; (void)i; *a = 3; return K_OK; }
static tresult p_cansize(void *t, int32_t s) { (void)t; return s == 0 ? K_OK : K_FALSE; }
static uint32_t p_latency(void *t) { return (uint32_t)plain(P_DELAY, FROM_P(t)->norm[P_DELAY]); }
static tresult p_setup(void *t, PSetup *s) { (void)t; return s->size == 0 ? K_OK : K_FALSE; }
static tresult p_processing(void *t, uint8_t s) { (void)t; (void)s; return K_OK; }
static tresult p_process(void *t, PData *d) {
    Obj *o = FROM_P(t);
    if (d->ichg) {
        ChangesV *cv = *(ChangesV **)d->ichg;
        int32_t n = cv->count(d->ichg);
        for (int32_t i = 0; i < n; i++) {
            void *q = cv->data(d->ichg, i);
            QueueV *qv = *(QueueV **)q;
            uint32_t id = qv->id(q);
            int32_t k = qv->count(q), off;
            double v;
            if (k > 0 && qv->get(q, k - 1, &off, &v) == K_OK && id >= 1 && id <= NP && id != P_PEAK) {
                if (id == P_GAIN && v != o->pgain) { o->echo_gain = o->pgain; o->pgain = v; }   /* the OLD value */
                o->norm[id] = v;
            }
        }
    }
    if (d->nin < 1 || d->nout < 1) return K_OK;
    float **in = d->in[0].buf, **out = d->out[0].buf;
    int n = d->n;
    if (o->esc) {
        if (!o->probed) {
            o->probed = 1;
            escape_probes(o->esc_v);
            for (uint32_t k = 0; k < ESC_N && d->ochg; k++) {   /* verdicts reported like meters */
                uint32_t id = 100 + k;
                int32_t idx;
                void *q = (*(ChangesV **)d->ochg)->add(d->ochg, &id, &idx);
                if (q) (*(QueueV **)q)->add(q, 0, o->esc_v[k], &idx);
            }
        }
        for (int c = 0; c < 2; c++) memcpy(out[c], in[c], n * sizeof(float));
        return K_OK;
    }
    if (o->echo_gain >= 0 && d->ochg) {                  /* stale echo (see the header) */
        uint32_t id = P_GAIN;
        int32_t idx;
        void *q = (*(ChangesV **)d->ochg)->add(d->ochg, &id, &idx);
        if (q) (*(QueueV **)q)->add(q, 0, o->echo_gain, &idx);
    }
    o->echo_gain = -1;
    double dl = plain(P_DELAY, o->norm[P_DELAY]);
    if (dl != o->last_delay) {
        o->last_delay = dl;
        if (o->handler) (*(HandlerV **)o->handler)->restart(o->handler, 1 << 3);
    }
    double mode = plain(P_MODE, o->norm[P_MODE]);
    float g = (float)plain(P_GAIN, o->norm[P_GAIN]) * (mode == 1 ? -1.f : mode == 2 ? 0.f : 1.f);
    uint32_t dd = (uint32_t)dl;
    double pk = plain(P_PEAK, o->norm[P_PEAK]);
    for (int i = 0; i < n; i++) {
        for (int c = 0; c < 2; c++) {
            float x = in[c][i];
            if (fabsf(x) > pk) pk = fabsf(x);
            o->ring[c][o->pos] = x;
            out[c][i] = g * o->ring[c][(o->pos + RING - dd) % RING];
        }
        o->pos = (o->pos + 1) % RING;
    }
    if (normv(P_PEAK, pk) != o->norm[P_PEAK]) {
        o->norm[P_PEAK] = normv(P_PEAK, pk > 10 ? 10 : pk);
        if (d->ochg) {
            uint32_t id = P_PEAK;
            int32_t idx;
            void *q = (*(ChangesV **)d->ochg)->add(d->ochg, &id, &idx);
            if (q) (*(QueueV **)q)->add(q, 0, o->norm[P_PEAK], &idx);
        }
    }
    return K_OK;
}
static uint32_t p_tail(void *t) { (void)t; return 0; }
static const struct { Unk u; void *f[8]; } VP = {{p_qi, p_add, p_rel}, {(void *)p_arr, (void *)p_getarr, (void *)p_cansize,
    (void *)p_latency, (void *)p_setup, (void *)p_processing, (void *)p_process, (void *)p_tail}};

/* --- IEditController --- */
static tresult e_qi(void *t, const uint8_t *i, void **o) { return o_qi(FROM_E(t), i, o); }
static uint32_t e_add(void *t) { return ++FROM_E(t)->refs; }
static uint32_t e_rel(void *t) { return o_rel(FROM_E(t)); }
static tresult e_init(void *t, void *c) { (void)t; (void)c; return K_OK; }
static tresult e_term(void *t) { (void)t; return K_OK; }
static tresult e_setcompstate(void *t, void *s) { (void)t; (void)s; return K_OK; }  /* same object: already set */
static tresult e_setstate(void *t, void *s) { (void)t; (void)s; return K_OK; }
static tresult e_getstate(void *t, void *s) { (void)t; (void)s; return K_OK; }
static int32_t e_count(void *t) { return FROM_E(t)->esc ? ESC_N : NP; }
static tresult e_info(void *t, int32_t i, PInfo *p) {
    Obj *o = FROM_E(t);
    memset(p, 0, sizeof *p);
    if (o->esc) {
        if (i < 0 || i >= ESC_N) return K_FALSE;
        p->id = 100 + i; u16(p->title, ESC_NAMES[i]); p->steps = 1; p->flags = 1 << 1;
        return K_OK;
    }
    switch (i) {
    case 0: p->id = P_GAIN; u16(p->title, "Gain"); u16(p->units, "x"); p->def = 0.5; p->flags = 1; break;
    case 1: p->id = P_DELAY; u16(p->title, "Delay"); u16(p->units, "smp"); p->steps = RING - 1; p->flags = 1; break;
    case 2: p->id = P_MODE; u16(p->title, "Mode"); p->steps = 2; p->flags = 1 | (1 << 3); break;
    case 3: p->id = P_PEAK; u16(p->title, "Peak"); p->flags = 1 << 1; break;
    case 4: p->id = P_BYPASS; u16(p->title, "Bypass"); p->steps = 1; p->flags = 1 | (1 << 16); break;
    default: return K_FALSE;
    }
    return K_OK;
}
static tresult e_str(void *t, uint32_t id, double v, uint16_t *s) {
    (void)t;
    char b[64];
    static const char *const modes[] = {"Clean", "Invert", "Mute"};
    if (id == P_MODE) snprintf(b, sizeof b, "%s", modes[(int)plain(P_MODE, v)]);
    else snprintf(b, sizeof b, "%g", plain((int)id, v));
    u16(s, b);
    return K_OK;
}
static tresult e_val(void *t, uint32_t id, const uint16_t *s, double *v) { (void)t; (void)id; (void)s; *v = 0; return K_FALSE; }
static double e_toplain(void *t, uint32_t id, double n) { (void)t; return plain((int)id, n); }
static double e_tonorm(void *t, uint32_t id, double p) { (void)t; return normv((int)id, p); }
static double e_get(void *t, uint32_t id) {
    Obj *o = FROM_E(t);
    if (o->esc) return id >= 100 && id < 100 + ESC_N && o->probed ? o->esc_v[id - 100] : 0;
    return id >= 1 && id <= NP ? o->norm[id] : 0;
}
static tresult e_set(void *t, uint32_t id, double v) {
    Obj *o = FROM_E(t);
    if (o->esc || id < 1 || id > NP || id == P_PEAK) return K_OK;
    if (id == P_MODE && v == o->norm[P_MODE]) o->norm[P_GAIN] = 0.5;   /* same Mode again: Gain back to default (1) */
    o->norm[id] = v;
    return K_OK;
}
static tresult e_handler(void *t, void *h) { FROM_E(t)->handler = h; return K_OK; }
static void *e_view(void *t, const char *n) { (void)t; (void)n; return NULL; }
static const struct { Unk u; void *f[15]; } VE = {{e_qi, e_add, e_rel}, {(void *)e_init, (void *)e_term, (void *)e_setcompstate,
    (void *)e_setstate, (void *)e_getstate, (void *)e_count, (void *)e_info, (void *)e_str, (void *)e_val, (void *)e_toplain,
    (void *)e_tonorm, (void *)e_get, (void *)e_set, (void *)e_handler, (void *)e_view}};

/* --- factory --- */
typedef struct { const void *v; } Factory;
static tresult f_qi(void *t, const uint8_t *i, void **o) {
    if (same(i, I_FUNKNOWN) || same(i, I_FACTORY)) { *o = t; return K_OK; }
    *o = NULL;
    return K_NOINTF;
}
static uint32_t f_addrel(void *t) { (void)t; return 1; }
static tresult f_info(void *t, FactInfo *i) { (void)t; memset(i, 0, sizeof *i); strcpy(i->vendor, "Bobi tests"); return K_OK; }
static int32_t f_count(void *t) { (void)t; return 2; }
static tresult f_class(void *t, int32_t i, ClassInfo *c) {
    (void)t;
    if (i < 0 || i > 1) return K_FALSE;
    memset(c, 0, sizeof *c); memcpy(c->cid, i == 0 ? C_DG : C_ESC, 16); c->card = 0x7FFFFFFF;
    strcpy(c->cat, "Audio Module Class"); strcpy(c->name, i == 0 ? "BobiTest VST3 Delay Gain" : "BobiTest VST3 Escape");
    return K_OK;
}
static tresult f_create(void *t, const uint8_t *cid, const uint8_t *iid, void **out) {
    (void)t;
    int esc = same(cid, C_ESC);
    if (!esc && !same(cid, C_DG)) { *out = NULL; return K_FALSE; }
    Obj *o = calloc(1, sizeof *o);
    o->vc = &VC; o->vp = &VP; o->ve = &VE; o->refs = 0; o->esc = esc;
    o->norm[P_GAIN] = 0.5;
    o->echo_gain = -1;
    o->pgain = 0.5;
    o->ring[0] = calloc(RING, 4); o->ring[1] = calloc(RING, 4);
    tresult r = o_qi(o, iid, out);
    if (r != K_OK) { free(o->ring[0]); free(o->ring[1]); free(o); }
    return r;
}
static const struct { Unk u; void *f[4]; } VF = {{f_qi, f_addrel, f_addrel}, {(void *)f_info, (void *)f_count, (void *)f_class, (void *)f_create}};
static Factory FACTORY = {&VF};
__attribute__((visibility("default"))) int ModuleEntry(void *h) { (void)h; return 1; }
__attribute__((visibility("default"))) int ModuleExit(void) { return 1; }
__attribute__((visibility("default"))) void *GetPluginFactory(void) { return &FACTORY; }
