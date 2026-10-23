#!/usr/bin/env python3
"""INTERFACES NATIVES VST3 et CLAP (plugin plugin_gui 0.1.4, 2026-10-18) — garde hors ligne.

L'éditeur d'un VST3 (IPlugView) ou la GUI d'un CLAP (clap.gui) n'existent pas sans une instance du plugin :
bobi_ui_host crée la SIENNE (image/vst3_gui.h, image/clap_gui.h), lui donne l'ÉTAT de l'instance de la console,
la nourrit de silence, et la tient au pas de la console par le relais commun (image/native_fmt.h). Ce qui est vérifié :

  1. hôte (uihost.c compilé avec -DPROBE_ONLY, sans X ni lilv), contre de FAUX plugins écrits ici en C — un VST3 à
     composant et contrôleur séparés (connectés par IMessage, éditeur qui passe par IRunLoop et IPlugFrame), un CLAP
     (clap.gui, timer-support, params, state, audio-ports) — conduits par le protocole réel (préambule `d` / `s`,
     lignes `v`, `w`, `geom`, `ready`) :
       a. l'état de la console arrive tel quel (composant, contrôleur ; blob clap.state), octet pour octet ;
       b. un geste dans l'interface part en valeur CONSOLE (VST3 : plain, ou index de pas d'un paramètre à pas dont
          les valeurs plain ne sont pas les entiers 0..n ; CLAP : plain) ; le contrôleur et le processeur reçoivent
          une valeur de la console (setParamNormalized ET IParameterChanges du bloc suivant ; CLAP : événement du bloc) ;
       c. règle 2 : une valeur que les deux côtés tiennent déjà n'est jamais renvoyée, ni dans un sens ni dans l'autre ;
       d. règle 3 : l'écho immédiat d'une valeur posée (JUCE ré-annonce ce que l'hôte pose) ne repart pas ;
       e. règle 4 : dans une rafale de la console, le paramètre à pas (type) passe AVANT les continus ;
       f. règle 5 : ce que le plugin change de lui-même part — après un changement de type (le faux « Surge » réinitialise
          un paramètre dans process() et l'annonce), après restartComponent(kParamValuesChanged) / rescan ;
       g. un paramètre dont les bornes diffèrent de celles de la console n'est relayé dans AUCUN sens ; une sortie
          (mesure) ne part jamais vers la console ; le bypass n'est pas exposé ;
       h. taille : resizeView / request_resize -> `geom W H resize`, notre fenêtre suit, onSize rappelé ;
          canResize / can_resize publiés en `fixed` ;
       i. ce que le plugin imprime sur stdout ne se mêle PAS au protocole ;
       j. CLAP actif : les gestes sortent par process() (DPF ne les rend QUE là) ; le plugin est sur le fil audio
          pendant process et seulement là ; CLAP qui refuse activate() : tout passe par params.flush() ;
       k. VST3 et CLAP : les descripteurs et minuteries que le plugin confie à l'hôte sont servis (IRunLoop,
          posix-fd / timer-support) ;
       o. (0.1.6) setupProcessing AVANT l'état de la console — LSP 1.2.35 divise par sa fréquence dès l'état reçu ;
       p. (0.1.6) catalogue `list-file` : un éditeur construit dans createView avec l'affichage (LSP) n'est pas `no_ui`
          faute d'affichage (`no_display`) ; un plugin vraiment sans éditeur reste `no_ui` ; un paquet de 396 classes
          est listé en entier ;
  2. service (src/service.py, src/link.py) : préambule (lignes filtrées, état base64 vérifié), catalogue des fichiers
     CLAP / VST3 (une URI d'un autre format refusée, un fichier remplacé relu ; 0.1.6 : paquet VST3 listé sur
     l'affichage du catalogue, `no_display` jamais gardé, affichage arrêté après le balayage, /catalog jamais bloqué
     par un balayage, publication au fil du balayage), VST3 / CLAP : vue de la console lue
     AVANT de créer le plugin, rien de poussé à l'ouverture, pas d'atomes ; LV2 : comme avant (tout poussé une fois,
     atomes) — le panneau et le chemin LV2 sont inchangés ; l'état de la console est demandé une fois.

plugin_gui est un dépôt à part : sans lui (CI), tout est SAUTÉ et le test le dit. Sans gcc, la partie 1 ÉCHOUE.
Mutations faites le 2026-10-18 : voir docs/chantiers/PROTO_GUI_PLUGINS.md.

    ./venv/bin/python tests/verif_native_gui_formats.py
"""
import base64
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.environ.get("PLUGIN_GUI_DIR") or os.path.join(_R, "plugins", "plugin_gui")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


if not os.path.isfile(os.path.join(GUI, "image", "native_fmt.h")):
    print("  --   plugin plugin_gui absent ou antérieur à 0.1.4 (%s) : tout est SAUTÉ" % GUI)
    sys.exit(0)
TMP = tempfile.mkdtemp(prefix="verif_ng_formats_")

# ====================================================================== 1. hôte contre de faux plugins
C_TEST = r'''
#define PROBE_ONLY
#define main uihost_main
#include "uihost.c"
#undef main
#define LOG(...) do { fprintf(stderr, "FAKE " __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } while (0)

/* ---------------- platform hooks */
static int g_win_w, g_win_h;
static unsigned long f_win_create(int w, int h) { g_win_w = w; g_win_h = h; LOG("win.create %dx%d", w, h); return 0xabc; }
static void f_win_resize(int w, int h) { g_win_w = w; g_win_h = h; LOG("win.resize %dx%d", w, h); }
static int f_win_measure(int *w, int *h) { *w = g_win_w; *h = g_win_h; return g_win_w > 0; }
static int f_win_fd(void) { return -1; }
static int f_win_pump(void) { return 0; }
static double f_now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static void *f_dl_open(const char *p) { LOG("dlopen %s", p); return (void *)1; }
static void *f_dl_sym(void *h, const char *n);
static const NHost FAKE = { f_win_create, f_win_resize, f_win_measure, f_win_fd, f_win_pump, f_dl_open, f_dl_sym, f_now };

/* ================= fake VST3 (separate component + controller, as JUCE / DPF) ================= */
typedef struct { V3Unk u; v3_res (*b)(V3P, uint32_t); v3_res (*p)(V3P, uint32_t, double); v3_res (*e)(V3P, uint32_t); v3_res (*r)(V3P, int32_t); } HandlerVt;
typedef struct { V3Unk u; v3_res (*resize)(V3P, V3P, V3Rect *); } FrameVt;
typedef struct { V3Unk u; v3_res (*rf)(V3P, V3P, int); v3_res (*uf)(V3P, V3P); v3_res (*rt)(V3P, V3P, uint64_t); v3_res (*ut)(V3P, V3P); } RunLoopVt;
typedef struct { V3Unk u; v3_res (*r)(V3P, V3P, int32_t, int32_t *); } StreamVt;
typedef struct { V3Unk u; int32_t (*count)(V3P); V3P (*data)(V3P, int32_t); } ChangesVt;
typedef struct { V3Unk u; uint32_t (*id)(V3P); int32_t (*count)(V3P); v3_res (*get)(V3P, int32_t, int32_t *, double *); } QueueVt;
typedef struct { V3Unk u; v3_res (*n)(V3P, uint16_t *); v3_res (*c)(V3P, const uint8_t *, const uint8_t *, V3P *); } HostVt;
typedef struct { V3Unk u; const char *(*g)(V3P); void (*s)(V3P, const char *); V3P (*a)(V3P); } MsgVt;
typedef struct { V3Unk u; v3_res (*si)(V3P, const char *, int64_t); v3_res (*gi)(V3P, const char *, int64_t *); } AttrVt;

static const uint8_t COMP_CID[16] = {0xC0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
static const uint8_t CTRL_CID[16] = {0xC1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
typedef struct { const void *vt; } FObj;
static V3P g_handler, g_frame, g_hostctx;
/* JUCE: what the host sets comes back as performEdit, LATE (from its timer): queued, sent at the next tick */
static struct { uint32_t id; double x; } g_echoq[16];
static int g_necho;
static struct FP { uint32_t id; int32_t steps, flags; double lo, hi, norm; } FP[] = {
    {10, 0, 0, -60, 10, 0.2},          /* gain: plain -60..10 */
    {20, 3, 0, 0, 3, 0.0},             /* type: 3 steps, plain 0..3 = the integers -> console plain 0..3; reads 0 while the
                                          console's state says 1 (a JUCE controller not synced yet) */
    {30, 2, 0, 100, 200, 0.0},         /* mode: 2 steps, plain 100..200 -> console = STEP INDEX 0..2 */
    {40, 0, 1 << 1, 0, 1, 0.0},        /* meter: read-only -> console output */
    {50, 0, 0, 0, 1, 0.0},             /* mismatch: the console says 0..5 */
    {60, 1, 1 << 16, 0, 1, 0.0},       /* bypass: not exposed */
};
#define NFP (int)(sizeof FP / sizeof FP[0])
static struct FP *fp(uint32_t id) { for (int i = 0; i < NFP; i++) if (FP[i].id == id) return &FP[i]; return NULL; }
static uint32_t f_ref(V3P t) { (void)t; return 1; }
static FObj g_fcomp, g_fctrl, g_fproc, g_fcp_comp, g_fcp_ctrl, g_fview, g_ftimer, g_fevh, g_ffactory;

static void read_stream(const char *what, V3P s) {
    char buf[256]; int32_t got = 0, n = 0;
    while (V3VT(s, StreamVt)->r(s, buf + n, 1, &got) == V3_OK && got == 1 && n < 255) n++;
    buf[n] = 0;
    LOG("%s <%s>", what, buf);
}
/* component */
static v3_res comp_q(V3P t, const uint8_t *iid, V3P *o);
static v3_res comp_init(V3P t, V3P ctx) { (void)t; g_hostctx = ctx; LOG("comp.init"); return V3_OK; }
static v3_res comp_term(V3P t) { (void)t; LOG("comp.terminate"); return V3_OK; }
static v3_res comp_ccid(V3P t, uint8_t *c) { (void)t; memcpy(c, CTRL_CID, 16); return V3_OK; }
static v3_res comp_iomode(V3P t, int32_t m) { (void)t; (void)m; return V3_OK; }
static int32_t comp_nbus(V3P t, int32_t media, int32_t dir) { (void)t; (void)dir; return media == 0 ? 1 : 0; }
static v3_res comp_bus(V3P t, int32_t m, int32_t d, int32_t i, void *bi) { (void)t; (void)m; (void)d; (void)i; ((V3BusInfo *)bi)->channel_count = 2; return V3_OK; }
static v3_res comp_route(V3P t, V3P a, V3P b) { (void)t; (void)a; (void)b; return V3_NOT_IMPL; }
static v3_res comp_actbus(V3P t, int32_t m, int32_t d, int32_t i, uint8_t on) { (void)t; (void)m; (void)i; LOG("comp.bus dir %d on %d", d, on); return V3_OK; }
static v3_res comp_active(V3P t, uint8_t on) { (void)t; LOG("comp.active %d", on); return V3_OK; }
/* LSP 1.2.35 applies a restored state at once and divides by its sample rate: before setupProcessing, SIGFPE */
static int g_setup_done;
static v3_res comp_setstate(V3P t, V3P s) {
    (void)t;
    if (!g_setup_done) LOG("comp.state BEFORE setupProcessing (LSP: SIGFPE)");
    read_stream("comp.state", s);
    return V3_OK;
}
static v3_res comp_getstate(V3P t, V3P s) { (void)t; (void)s; return V3_OK; }
static const V3Component COMP_VT = { { comp_q, f_ref, f_ref }, comp_init, comp_term, comp_ccid, comp_iomode, comp_nbus, comp_bus,
                                     comp_route, comp_actbus, comp_active, comp_setstate, comp_getstate };
/* processor: a « Surge »: a new type resets the gain, applied in process() and announced (performEdit) */
static v3_res proc_q(V3P t, const uint8_t *iid, V3P *o) { return comp_q(t, iid, o); }
static v3_res pr_arr(V3P t, uint64_t *i, int32_t ni, uint64_t *o, int32_t no) { (void)t; LOG("proc.arrangement %d:%llu %d:%llu", ni, ni ? (unsigned long long)i[0] : 0ull, no, no ? (unsigned long long)o[0] : 0ull); return V3_OK; }
static v3_res pr_garr(V3P t, int32_t d, int32_t i, uint64_t *a) { (void)t; (void)d; (void)i; *a = 3; return V3_OK; }
static v3_res pr_can(V3P t, int32_t s) { (void)t; return s == 0 ? V3_OK : V3_FALSE; }
static uint32_t pr_lat(V3P t) { (void)t; return 0; }
static v3_res pr_setup(V3P t, V3Setup *s) { (void)t; g_setup_done = 1; LOG("proc.setup %d %.0f", s->max_samples_per_block, s->sample_rate); return V3_OK; }
static v3_res pr_proc_on(V3P t, uint8_t on) { (void)t; LOG("proc.processing %d", on); return V3_OK; }
static int g_blocks;
static v3_res pr_process(V3P t, V3ProcessData *d) {
    (void)t;
    g_blocks++;
    if (d->num_samples != V3_FRAMES || d->num_outputs != 1 || d->outputs[0].num_channels != 2) LOG("proc.BAD block");
    V3P ch = d->input_parameter_changes;
    int32_t n = ch ? V3VT(ch, ChangesVt)->count(ch) : 0;
    for (int32_t i = 0; i < n; i++) {
        V3P q = V3VT(ch, ChangesVt)->data(ch, i);
        int32_t off; double v;
        if (!q || V3VT(q, QueueVt)->get(q, 0, &off, &v) != V3_OK) continue;
        uint32_t id = V3VT(q, QueueVt)->id(q);
        LOG("proc.param %u %.4f", id, v);
        if (id == 20) {                                  /* new type: the gain is reset, and said */
            fp(10)->norm = 0.1;
            V3VT(g_handler, HandlerVt)->p(g_handler, 10, 0.1);
        }
    }
    return V3_OK;
}
static uint32_t pr_tail(V3P t) { (void)t; return 0; }
static const V3Processor PROC_VT = { { proc_q, f_ref, f_ref }, pr_arr, pr_garr, pr_can, pr_lat, pr_setup, pr_proc_on, pr_process, pr_tail };
/* connection points: the component hands the controller a message (JUCE gives its processor that way) */
static v3_res cp_q(V3P t, const uint8_t *iid, V3P *o) { *o = t; (void)iid; return V3_OK; }
static v3_res cp_connect(V3P t, V3P other) {
    if (t != &g_fcp_comp) return V3_OK;
    V3P m = NULL;
    if (V3VT(g_hostctx, HostVt)->c(g_hostctx, V3_MESSAGE, V3_MESSAGE, &m) != V3_OK || !m) { LOG("msg.none"); return V3_OK; }
    V3VT(m, MsgVt)->s(m, "handover");
    V3P a = V3VT(m, MsgVt)->a(m);
    V3VT(a, AttrVt)->si(a, "ptr", 4242);
    V3VT(other, V3ConnPoint)->notify(other, m);
    V3VT(m, V3Unk)->release(m);
    return V3_OK;
}
static v3_res cp_disc(V3P t, V3P o) { (void)t; (void)o; LOG("disconnect"); return V3_OK; }
static v3_res cp_notify(V3P t, V3P m) {
    (void)t;
    int64_t v = 0;
    V3P a = V3VT(m, MsgVt)->a(m);
    V3VT(a, AttrVt)->gi(a, "ptr", &v);
    LOG("ctrl.notify %s %lld", V3VT(m, MsgVt)->g(m), (long long)v);
    return V3_OK;
}
static const V3ConnPoint CP_VT = { { cp_q, f_ref, f_ref }, cp_connect, cp_disc, cp_notify };
static v3_res comp_q(V3P t, const uint8_t *iid, V3P *o) {
    (void)t;
    if (v3_same(iid, V3_COMPONENT) || v3_same(iid, V3_FUNKNOWN)) { *o = &g_fcomp; return V3_OK; }
    if (v3_same(iid, V3_AUDIO_PROCESSOR)) { *o = &g_fproc; return V3_OK; }
    if (v3_same(iid, V3_CONNECTION_POINT)) { *o = &g_fcp_comp; return V3_OK; }
    *o = NULL; return V3_NO_IFACE;
}
/* controller */
static v3_res ctrl_q(V3P t, const uint8_t *iid, V3P *o) {
    (void)t;
    if (v3_same(iid, V3_EDIT_CONTROLLER) || v3_same(iid, V3_FUNKNOWN)) { *o = &g_fctrl; return V3_OK; }
    if (v3_same(iid, V3_CONNECTION_POINT)) { *o = &g_fcp_ctrl; return V3_OK; }
    *o = NULL; return V3_NO_IFACE;
}
static v3_res ct_init(V3P t, V3P c) { (void)t; (void)c; printf("PLUGIN NOISE on stdout\n"); fflush(stdout); return V3_OK; }
static v3_res ct_term(V3P t) { (void)t; LOG("ctrl.terminate"); return V3_OK; }
static v3_res ct_cstate(V3P t, V3P s) { (void)t; read_stream("ctrl.compstate", s); return V3_OK; }
static v3_res ct_state(V3P t, V3P s) { (void)t; read_stream("ctrl.state", s); return V3_OK; }
static v3_res ct_gstate(V3P t, V3P s) { (void)t; (void)s; return V3_OK; }
static int32_t ct_count(V3P t) { (void)t; return NFP; }
static v3_res ct_info(V3P t, int32_t i, V3ParamInfo *pi) {
    (void)t;
    if (i < 0 || i >= NFP) return V3_INVALID;
    memset(pi, 0, sizeof *pi);
    pi->id = FP[i].id; pi->step_count = FP[i].steps; pi->flags = FP[i].flags;
    return V3_OK;
}
static v3_res ct_str(V3P t, uint32_t id, double x, uint16_t *s) { (void)t; (void)id; (void)x; s[0] = 0; return V3_OK; }
static v3_res ct_val(V3P t, uint32_t id, const uint16_t *s, double *x) { (void)t; (void)id; (void)s; (void)x; return V3_FALSE; }
static double ct_plain(V3P t, uint32_t id, double x) { (void)t; struct FP *p = fp(id); return p ? p->lo + (p->hi - p->lo) * x : x; }
static double ct_norm(V3P t, uint32_t id, double v) { (void)t; struct FP *p = fp(id); return p ? (v - p->lo) / (p->hi - p->lo) : v; }
static double ct_get(V3P t, uint32_t id) { (void)t; struct FP *p = fp(id); return p ? p->norm : 0; }
static v3_res ct_set(V3P t, uint32_t id, double x) {
    (void)t;
    struct FP *p = fp(id);
    if (!p) return V3_INVALID;
    p->norm = x;
    LOG("ctrl.set %u %.4f", id, x);
    if (g_necho < 16) { g_echoq[g_necho].id = id; g_echoq[g_necho].x = x; g_necho++; }
    return V3_OK;
}
static v3_res ct_handler(V3P t, V3P h) { (void)t; g_handler = h; return V3_OK; }
/* catalog modes (`list`): an editor that LSP builds in createView with the display it opens THERE (none: no view),
 * an effect with no editor at all, the usual one; g_list_display = what the host told v3_list */
enum { ED_USUAL, ED_NEEDS_DISPLAY, ED_NONE };
static int g_editor = ED_USUAL, g_list_display = 1, g_many;
static V3P ct_view(V3P t, const char *name) {
    (void)t;
    LOG("ctrl.view %s", name);
    if (g_editor == ED_NONE || (g_editor == ED_NEEDS_DISPLAY && !g_list_display)) return NULL;
    return &g_fview;
}
static const V3Controller CTRL_VT = { { ctrl_q, f_ref, f_ref }, ct_init, ct_term, ct_cstate, ct_state, ct_gstate, ct_count, ct_info,
                                      ct_str, ct_val, ct_plain, ct_norm, ct_get, ct_set, ct_handler, ct_view };
/* the editor: everything it does happens from a timer the host serves through IRunLoop */
static int g_pipe[2], g_tick;
static v3_res view_q(V3P t, const uint8_t *iid, V3P *o) { *o = t; (void)iid; return V3_OK; }
static v3_res vw_plat(V3P t, const char *p) { (void)t; return !strcmp(p, "X11EmbedWindowID") ? V3_OK : V3_FALSE; }
static v3_res vw_attached(V3P t, void *parent, const char *type) {
    (void)t;
    LOG("view.attached 0x%lx %s", (unsigned long)(uintptr_t)parent, type);
    V3P rl = NULL;
    if (V3VT(g_frame, V3Unk)->query_interface(g_frame, V3_RUN_LOOP, &rl) != V3_OK || !rl) { LOG("runloop.none"); return V3_OK; }
    V3VT(rl, RunLoopVt)->rt(rl, &g_ftimer, 20);
    if (!pipe(g_pipe)) V3VT(rl, RunLoopVt)->rf(rl, &g_fevh, g_pipe[0]);
    return V3_OK;
}
static v3_res vw_removed(V3P t) { (void)t; LOG("view.removed"); return V3_OK; }
static v3_res vw_wheel(V3P t, float d) { (void)t; (void)d; return V3_OK; }
static v3_res vw_key(V3P t, uint16_t k, int16_t c, int16_t m) { (void)t; (void)k; (void)c; (void)m; return V3_OK; }
static v3_res vw_size(V3P t, V3Rect *r) { (void)t; r->left = r->top = 0; r->right = 400; r->bottom = 300; return V3_OK; }
static v3_res vw_onsize(V3P t, V3Rect *r) { (void)t; LOG("view.onsize %dx%d", r->right - r->left, r->bottom - r->top); return V3_OK; }
static v3_res vw_focus(V3P t, uint8_t f) { (void)t; (void)f; return V3_OK; }
static v3_res vw_frame(V3P t, V3P f) { (void)t; g_frame = f; return V3_OK; }
static v3_res vw_canresize(V3P t) { (void)t; return V3_OK; }
static v3_res vw_check(V3P t, V3Rect *r) { (void)t; (void)r; return V3_OK; }
static const V3View VIEW_VT = { { view_q, f_ref, f_ref }, vw_plat, vw_attached, vw_removed, vw_wheel, vw_key, vw_key, vw_size,
                                vw_onsize, vw_focus, vw_frame, vw_canresize, vw_check };
static void tm_on(V3P t) {
    (void)t;
    HandlerVt const *h = V3VT(g_handler, HandlerVt);
    for (int i = 0; i < g_necho; i++) h->p(g_handler, g_echoq[i].id, g_echoq[i].x);
    g_necho = 0;
    switch (++g_tick) {
    case 2: if (write(g_pipe[1], "x", 1) != 1) LOG("pipe"); break;
    case 3: fp(10)->norm = 0.5; h->b(g_handler, 10); h->p(g_handler, 10, 0.5); h->e(g_handler, 10); break;  /* gain -> -25 */
    case 5: { V3Rect r = {0, 0, 500, 350}; V3VT(g_frame, FrameVt)->resize(g_frame, &g_fview, &r); } break;
    case 7: fp(20)->norm = 1.0 / 3; fp(30)->norm = 1.0; h->r(g_handler, 1 << 2); break;  /* synced; mode -> step 2 */
    case 8: fp(30)->norm = 0.5; break;                                       /* mode -> step 1, silently (no announce) */
    case 9: h->p(g_handler, 40, 0.7); h->p(g_handler, 50, 0.3); h->p(g_handler, 60, 1.0); break;     /* never relayed */
    case 11: printf("PLUGIN NOISE again\n"); fflush(stdout); LOG("script done"); break;
    }
}
static const V3TimerHandler TIMER_VT = { { view_q, f_ref, f_ref }, tm_on };
static void evh_on(V3P t, int fd) { (void)t; char c; if (read(fd, &c, 1) == 1) LOG("fd.ready %c", c); }
static const V3EventHandler EVH_VT = { { view_q, f_ref, f_ref }, evh_on };
/* factory, module */
static v3_res fa_q(V3P t, const uint8_t *iid, V3P *o) { *o = t; (void)iid; return V3_OK; }
static v3_res fa_info(V3P t, void *i) { (void)t; (void)i; return V3_OK; }
/* g_many: a bundle like LSP 1.2.35 — 198 effects, each with its controller class: 396 classes */
static int32_t fa_count(V3P t) { (void)t; return g_many ? 396 : 1; }
static v3_res fa_class(V3P t, int32_t i, V3ClassInfo *ci) {
    (void)t;
    memset(ci, 0, sizeof *ci);
    memcpy(ci->cid, COMP_CID, 16);
    strcpy(ci->category, "Audio Module Class");
    strcpy(ci->name, "Fake Effect");
    if (g_many) {
        ci->cid[14] = (uint8_t)(i >> 1); ci->cid[15] = (uint8_t)(i & 1);
        if (i & 1) { ci->cid[0] = 0xC1; strcpy(ci->category, "Component Controller Class"); }
        snprintf(ci->name, sizeof ci->name, "Fake %d", i >> 1);
    }
    return V3_OK;
}
static v3_res fa_create(V3P t, const uint8_t *cid, const uint8_t *iid, V3P *o) {
    (void)t; (void)iid;
    if (!memcmp(cid, COMP_CID, g_many ? 14 : 16)) { *o = &g_fcomp; return V3_OK; }
    if (!memcmp(cid, CTRL_CID, 16)) { *o = &g_fctrl; return V3_OK; }
    *o = NULL; return V3_FALSE;
}
static const V3Factory FACT_VT = { { fa_q, f_ref, f_ref }, fa_info, fa_count, fa_class, fa_create };
static int fake_module_entry(void *h) { (void)h; LOG("ModuleEntry"); return 1; }
static V3P fake_get_factory(void) { return &g_ffactory; }

/* ================= fake CLAP ================= */
static const clap_host_t *g_ch;
static double g_cv[5] = {0, 0.5, 3, 0, 0.2};
static int g_ctick, g_c_inactive, g_nout;
static clap_event_param_value_t g_cout[8];
static int g_audio_ok = -1;
static const void *chx(const char *id) { return g_ch->get_extension(g_ch, id); }
static void c_events(const clap_input_events_t *in, const clap_output_events_t *out, const char *via) {
    for (uint32_t i = 0; in && i < in->size(in); i++) {
        const clap_event_header_t *h = in->get(in, i);
        if (h->type != CLAP_EVENT_PARAM_VALUE) continue;
        const clap_event_param_value_t *e = (const void *)h;
        if (e->param_id < 5) g_cv[e->param_id] = e->value;
        LOG("clap.in %u %.4f via %s", e->param_id, e->value, via);
    }
    for (int i = 0; i < g_nout; i++) { out->try_push(out, &g_cout[i].header); LOG("clap.out %u via %s", g_cout[i].param_id, via); }
    g_nout = 0;
}
static bool cp_init(const clap_plugin_t *p) { (void)p; return true; }
static void cp_destroy(const clap_plugin_t *p) { (void)p; LOG("clap.destroy"); }
static bool cp_activate(const clap_plugin_t *p, double sr, uint32_t mn, uint32_t mx) { (void)p; LOG("clap.activate %.0f %u %u", sr, mn, mx); return !g_c_inactive; }
static void cp_deactivate(const clap_plugin_t *p) { (void)p; LOG("clap.deactivate"); }
static bool cp_start(const clap_plugin_t *p) { (void)p; return true; }
static void cp_stop(const clap_plugin_t *p) { (void)p; }
static void cp_reset(const clap_plugin_t *p) { (void)p; }
static int32_t cp_process(const clap_plugin_t *p, const void *pp) {
    (void)p;
    const clap_process_t *pr = pp;
    const struct { bool (*m)(const clap_host_t *); bool (*a)(const clap_host_t *); } *tc = chx("clap.thread-check");
    int a = tc->a(g_ch) && !tc->m(g_ch);
    if (g_audio_ok < 0 || !a) g_audio_ok = a;
    if (pr->frames_count != CP_FRAMES || pr->audio_outputs_count != 1 || pr->audio_outputs[0].channel_count != 2) LOG("clap.BAD block");
    c_events(pr->in_events, pr->out_events, "process");
    return 1;
}
static uint32_t pa_count(const clap_plugin_t *p) { (void)p; return 4; }
static bool pa_info(const clap_plugin_t *p, uint32_t i, clap_param_info_t *pi) {
    (void)p;
    static const struct { uint32_t id, flags; double mn, mx; } T[] = { {1, 0, 0, 1}, {2, 1, 0, 10}, {3, CLAP_PARAM_IS_BYPASS, 0, 1}, {4, 0, 0, 1} };
    if (i >= 4) return false;
    memset(pi, 0, sizeof *pi);
    pi->id = T[i].id; pi->flags = T[i].flags; pi->min_value = T[i].mn; pi->max_value = T[i].mx;
    return true;
}
static bool pa_get(const clap_plugin_t *p, uint32_t id, double *v) { (void)p; if (id >= 5) return false; *v = g_cv[id]; return true; }
static void pa_flush(const clap_plugin_t *p, const clap_input_events_t *in, const clap_output_events_t *out) {
    (void)p;
    const struct { bool (*m)(const clap_host_t *); bool (*a)(const clap_host_t *); } *tc = chx("clap.thread-check");
    if (!tc->m(g_ch)) LOG("clap.flush OFF the main thread");
    c_events(in, out, "flush");
}
static const clap_plugin_params_t C_PARAMS = { pa_count, pa_info, pa_get, NULL, NULL, pa_flush };
static bool st_load(const clap_plugin_t *p, const clap_istream_t *s) {
    (void)p; char b[64]; int64_t n = s->read(s, b, sizeof b - 1);
    b[n > 0 ? n : 0] = 0; LOG("clap.state <%s>", b); return true;
}
static const clap_plugin_state_t C_STATE = { NULL, st_load };
static void c_on_timer(const clap_plugin_t *p, uint32_t id) {
    (void)p; (void)id;
    const struct { void (*r)(const clap_host_t *, uint32_t); void (*c)(const clap_host_t *, uint32_t, uint32_t); void (*f)(const clap_host_t *); } *hp = chx("clap.params");
    const struct { void (*h)(const clap_host_t *); bool (*r)(const clap_host_t *, uint32_t, uint32_t); } *hg = chx("clap.gui");
    switch (++g_ctick) {
    case 3: g_cout[0] = (clap_event_param_value_t){ { sizeof(clap_event_param_value_t), 0, 0, CLAP_EVENT_PARAM_VALUE, 0 }, 1, NULL, -1, -1, -1, -1, 0.25 };
            g_cv[1] = 0.25; g_nout = 1; hp->f(g_ch); break;                                  /* a gesture: gain 0.25 */
    case 5: hg->r(g_ch, 320, 240); break;
    case 7: g_cv[2] = 7; hp->r(g_ch, CLAP_PARAM_RESCAN_VALUES); break;                      /* changed by itself */
    case 9: g_cout[0] = (clap_event_param_value_t){ { sizeof(clap_event_param_value_t), 0, 0, CLAP_EVENT_PARAM_VALUE, 0 }, 4, NULL, -1, -1, -1, -1, 0.5 };
            g_nout = 1; hp->f(g_ch); break;                                                  /* bounds differ: never relayed */
    case 11: printf("PLUGIN NOISE clap\n"); fflush(stdout); LOG("script done"); break;
    }
}
static const clap_plugin_timer_support_t C_TIMER = { c_on_timer };
static bool gu_api(const clap_plugin_t *p, const char *api, bool fl) { (void)p; return !strcmp(api, "x11") && !fl; }
static bool gu_create(const clap_plugin_t *p, const char *api, bool fl) {
    (void)p; (void)api; (void)fl; uint32_t id;
    const struct { bool (*r)(const clap_host_t *, uint32_t, uint32_t *); bool (*u)(const clap_host_t *, uint32_t); } *ht = chx("clap.timer-support");
    if (!ht || !ht->r(g_ch, 5, &id)) LOG("clap.timer refused");
    return true;
}
static void gu_destroy(const clap_plugin_t *p) { (void)p; LOG("clap.gui.destroy"); }
static bool gu_size(const clap_plugin_t *p, uint32_t *w, uint32_t *h) { (void)p; *w = 300; *h = 200; return true; }
static bool gu_canresize(const clap_plugin_t *p) { (void)p; return false; }
static bool gu_parent(const clap_plugin_t *p, const clap_window_t *w) { (void)p; LOG("clap.parent %s 0x%lx", w->api, w->x11); return true; }
static bool gu_show(const clap_plugin_t *p) { (void)p; return true; }
static const clap_plugin_gui_t C_GUI = { gu_api, NULL, gu_create, gu_destroy, NULL, gu_size, gu_canresize, NULL, NULL, NULL, gu_parent, NULL, NULL, gu_show, gu_show };
static uint32_t ap_count(const clap_plugin_t *p, bool in) { (void)p; (void)in; return 1; }
static bool ap_get(const clap_plugin_t *p, uint32_t i, bool in, clap_audio_port_info_t *pi) { (void)p; (void)i; (void)in; memset(pi, 0, sizeof *pi); pi->channel_count = 2; return true; }
static const clap_plugin_audio_ports_t C_PORTS = { ap_count, ap_get };
static const void *cp_ext(const clap_plugin_t *p, const char *id) {
    (void)p;
    if (!strcmp(id, "clap.params")) return &C_PARAMS;
    if (!strcmp(id, "clap.state")) return &C_STATE;
    if (!strcmp(id, "clap.gui")) return &C_GUI;
    if (!strcmp(id, "clap.timer-support")) return &C_TIMER;
    if (!strcmp(id, "clap.audio-ports")) return &C_PORTS;
    return NULL;
}
static void cp_main(const clap_plugin_t *p) { (void)p; }
static clap_plugin_descriptor_t C_DESC = { {1, 2, 0}, "fake.clap", "Fake CLAP", "", "", "", "", "", "", NULL };
static clap_plugin_t C_PLUGIN = { &C_DESC, NULL, cp_init, cp_destroy, cp_activate, cp_deactivate, cp_start, cp_stop, cp_reset,
                                  cp_process, cp_ext, cp_main };
static uint32_t cf_count(const clap_plugin_factory_t *f) { (void)f; return 1; }
static const clap_plugin_descriptor_t *cf_desc(const clap_plugin_factory_t *f, uint32_t i) { (void)f; (void)i; return &C_DESC; }
static const clap_plugin_t *cf_create(const clap_plugin_factory_t *f, const clap_host_t *h, const char *id) {
    (void)f; g_ch = h; return strcmp(id, "fake.clap") ? NULL : &C_PLUGIN;
}
static const clap_plugin_factory_t C_FACTORY = { cf_count, cf_desc, cf_create };
static bool ce_init(const char *p) { LOG("clap_entry.init %s", p); return true; }
static void ce_deinit(void) {}
static const void *ce_factory(const char *id) { return !strcmp(id, "clap.plugin-factory") ? &C_FACTORY : NULL; }
static const clap_plugin_entry_t C_ENTRY = { {1, 2, 0}, ce_init, ce_deinit, ce_factory };

static void *f_dl_sym(void *h, const char *n) {
    (void)h;
    if (!strcmp(n, "ModuleEntry")) return (void *)fake_module_entry;
    if (!strcmp(n, "GetPluginFactory")) return (void *)fake_get_factory;
    if (!strcmp(n, "clap_entry")) return (void *)&C_ENTRY;
    return NULL;
}

int main(int argc, char **argv) {
    g_fcomp.vt = &COMP_VT; g_fctrl.vt = &CTRL_VT; g_fproc.vt = &PROC_VT; g_fcp_comp.vt = &CP_VT; g_fcp_ctrl.vt = &CP_VT;
    g_fview.vt = &VIEW_VT; g_ftimer.vt = &TIMER_VT; g_fevh.vt = &EVH_VT; g_ffactory.vt = &FACT_VT;
    g_nh = &FAKE;
    nf_protocol_apart();
    if (!strcmp(argv[1], "list") && argc == 5) {       /* list <display 0|1> <usual|needs_display|none|many> <root> */
        g_list_display = atoi(argv[2]);
        g_editor = !strcmp(argv[3], "needs_display") ? ED_NEEDS_DISPLAY : !strcmp(argv[3], "none") ? ED_NONE : ED_USUAL;
        g_many = !strcmp(argv[3], "many");
        int rc = v3_list(argv[4], "Fake.vst3", g_list_display);
        fflush(g_out);
        return rc;
    }
    g_c_inactive = argc > 2 && !strcmp(argv[2], "inactive");
    if (nf_preamble(0) < 0) return 0;
    int rc = !strcmp(argv[1], "vst3") ? v3_run("vst3:Fake.vst3#C00102030405060708090A0B0C0D0E0F", 0)
                                      : cl_run("clap:sub/Fake.clap#fake.clap", 0);
    if (argv[1][0] == 'c') LOG("audio thread during process %d", g_audio_ok);
    LOG("blocks %d", g_blocks);
    return rc;
}
'''


class Host:
    """The compiled host + fake plugin, driven line by line like the service drives bobi_ui_host."""

    def __init__(self, exe, args, env):
        self.p = subprocess.Popen([exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env)
        self.out = []

    def send(self, *lines):
        self.p.stdin.write(("".join(l + "\n" for l in lines)).encode())
        self.p.stdin.flush()

    def until(self, pred, timeout=3.0):
        """reads protocol lines until pred(line) is true for one of them (or timeout); returns that line or None"""
        t_end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < t_end:
            r, _, _ = select.select([self.p.stdout], [], [], 0.05)
            if not r:
                continue
            c = os.read(self.p.stdout.fileno(), 4096)
            if not c:
                break
            buf += c
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode()
                self.out.append(s)
                if pred(s):
                    return s
        return None

    def finish(self):
        self.send("q")
        try:
            o, e = self.p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            o, e = self.p.communicate()
        self.out += o.decode().splitlines()
        self.err = e.decode().splitlines()
        return self.p.returncode


def fake(err, prefix):
    return [l[5:] for l in err if l.startswith("FAKE " + prefix)]


if not shutil.which("gcc"):
    ok(False, "1. gcc présent (l'hôte VST3 / CLAP n'est PAS vérifié)")
else:
    src = os.path.join(TMP, "fmt.c")
    exe = os.path.join(TMP, "fmt")
    open(src, "w").write(C_TEST)
    cc = subprocess.run(["gcc", "-O1", "-Wall", "-I", os.path.join(GUI, "image"), "-o", exe, src, "-lm", "-lpthread"],
                        capture_output=True, text=True)
    ok(cc.returncode == 0, "1. uihost.c (VST3 / CLAP) compile sans X ni lilv, avec les faux plugins", cc.stderr[-1500:])
    if cc.returncode == 0:
        os.makedirs(os.path.join(TMP, "vst3", "Fake.vst3", "Contents", "x86_64-linux"))
        open(os.path.join(TMP, "vst3", "Fake.vst3", "Contents", "x86_64-linux", "Fake.so"), "w").close()
        os.makedirs(os.path.join(TMP, "clap", "sub"))
        open(os.path.join(TMP, "clap", "sub", "Fake.clap"), "w").close()
        env = dict(os.environ, VST3_PATH="/nowhere:" + os.path.join(TMP, "vst3"), CLAP_PATH=os.path.join(TMP, "clap"))

        # ---------------- VST3
        state = b"bobistudio-vst3-state-1\n--component 4\nCOMP--controller 4\nCTRL--values 9\np10=-46\n\n"
        h = Host(exe, ["vst3"], env)
        h.send("d p10 -60 10 0 -46", "d p20 0 3 4 1", "d p30 0 2 4 0", "d p40 0 1 1 0", "d p50 0 5 0 0", "d p60 0 1 2 0",
               "s " + base64.b64encode(state).decode())
        ready = h.until(lambda l: l.startswith("ready "))
        h.send("v p20 1")                         # the console's value since the preamble; our controller still says 0
        ok(ready == "ready 400 300", "1v-h1. VST3 : prête à la taille de la vue (getSize 400 x 300)", (ready, h.out))
        ok("geom 400 300 resize 0" in h.out, "1v-h2. taille publiée « resize » (la vue l'a dite), redimensionnable (fixed 0)", h.out)
        h.until(lambda l: l == "w p30 2")
        h.until(lambda l: l.startswith("geom 500"), 1.0)
        # rule 2 / 3 / 4 from the console
        h.until(lambda l: l == "never", 0.15)    # tick 8 passed
        h.send("v p10 -25")                       # what the interface itself posted: both sides hold it
        time.sleep(0.15)
        h.send("v p30 1")                         # differs from what was exchanged, but our instance already holds it
        time.sleep(0.15)
        h.send("v p10 -18")                       # a fader moved in the fiche: -18 then -11, within one tick of
        time.sleep(0.004)                         # the plugin's timer — both echoes come back after -11 was set
        h.send("v p10 -11")
        time.sleep(0.15)
        h.send("v p10 -4\nv p20 3")               # ONE burst: the type (stepped) first
        time.sleep(0.3)
        h.send("v p50 3", "v p40 0.5")            # bounds differ: nothing ; a meter: shown
        time.sleep(0.3)
        h.until(lambda l: l == "never", 0.3)
        rc = h.finish()
        e = h.err
        sets = fake(e, "ctrl.set")
        procs = fake(e, "proc.param")
        ok(fake(e, "comp.state") == ["comp.state <COMP>"] and fake(e, "ctrl.compstate") == ["ctrl.compstate <COMP>"]
           and fake(e, "ctrl.state") == ["ctrl.state <CTRL>"],
           "1a. VST3 : l'état de la console arrive tel quel (composant -> composant et contrôleur, contrôleur -> contrôleur)",
           [l for l in e if "state" in l])
        ok(fake(e, "ctrl.notify") == ["ctrl.notify handover 4242"],
           "1k1. composant et contrôleur connectés, message (IMessage / IAttributeList de l'hôte) transmis", fake(e, "ctrl.notify"))
        ok("w p10 -25" in h.out and "geom 500 350 resize 0" in h.out and h.out.index("w p10 -25") < h.out.index("geom 500 350 resize 0"),
           "1b1. geste (performEdit 0.5) -> valeur plain de la console (-25), à l'instant (tick 3, avant le tick 5)", h.out)
        ok("w p30 2" in h.out, "1b2. paramètre à pas aux valeurs plain non entières : la console reçoit l'INDEX du pas (2)", h.out)
        ok(fake(e, "fd.ready") == ["fd.ready x"] and "geom 500 350 resize 0" in h.out,
           "1k2. IRunLoop : minuterie et descripteur du plugin servis ; 1h. resizeView -> geom 500 x 350 « resize »",
           (fake(e, "fd.ready"), h.out))
        ok("view.onsize 500x350" in [x for x in fake(e, "view.onsize")] and "win.resize 500x350" in fake(e, "win.resize"),
           "1h2. notre fenêtre suit, puis onSize rappelé (contrat d'hôte du SDK)", (fake(e, "view.onsize"), fake(e, "win.resize")))
        ok(not any(s.startswith("ctrl.set 10 0.5000") for s in sets) and h.out.count("w p10 -25") == 1,
           "1c1. règle 2 : la valeur que l'interface a postée, renvoyée par la console, n'est pas reposée", (sets, h.out))
        ok(not any(s.startswith("ctrl.set 20 0.3333") for s in sets),
           "1c3. règle 2 : une valeur de la console égale à celle du préambule n'est pas reposée, même si notre contrôleur "
           "(pas encore synchronisé) dit autre chose — c'est ce qui réinitialiserait les réglages d'un Surge", sets)
        ok(not any(s.startswith("ctrl.set 30") for s in sets),
           "1c4. règle 2 : une valeur que notre instance tient déjà n'est pas reposée", sets)
        ok("ctrl.set 10 0.7000" in sets and "proc.param 10 0.7000" in procs,
           "1b3. valeur de la console -> contrôleur (setParamNormalized) ET processeur (IParameterChanges du bloc)", (sets, procs))
        ok("ctrl.set 10 0.6000" in sets and "w p10 -11" not in h.out and "w p10 -18" not in h.out,
           "1d. règle 3 : l'écho TARDIF des valeurs posées ne repart pas, même celui d'une valeur déjà dépassée (-18 après -11)",
           (sets, h.out))
        i20 = next((i for i, s in enumerate(sets) if s.startswith("ctrl.set 20 ")), -1)
        i10 = next((i for i, s in enumerate(sets) if s.startswith("ctrl.set 10 0.8000")), -1)
        ok(0 <= i20 < i10, "1e. règle 4 : rafale « gain puis type » -> type posé d'abord", sets)
        ok("w p10 -53" in h.out, "1f1. règle 5 : le type appliqué dans process() réinitialise le gain, annoncé -> parti vers la console",
           h.out)
        ok(not any(s.startswith("ctrl.set 50") for s in sets) and not any(l.startswith("w p50") for l in h.out),
           "1g1. bornes différentes de la console : relayé dans AUCUN sens", (sets, h.out))
        ok(not any(l.startswith(("w p40", "w p60")) for l in h.out) and "ctrl.set 40 0.5000" in sets,
           "1g2. mesure : montrée à l'interface, jamais renvoyée ; bypass : non exposé", (sets, h.out))
        ok(not any("NOISE" in l for l in h.out) and any("NOISE" in l for l in e),
           "1i. ce que le plugin imprime sur stdout va sur stderr, jamais dans le protocole", [l for l in h.out + e if "NOISE" in l])
        ok("proc.arrangement 1:3 1:3" in fake(e, "proc.arrangement") and fake(e, "comp.active") == ["comp.active 1", "comp.active 0"]
           and any(l.startswith("blocks ") and int(l.split()[1]) > 5 for l in fake(e, "blocks")),
           "1j1. VST3 activé (stéréo), nourri de blocs silencieux, désactivé à la fermeture",
           (fake(e, "proc.arrangement"), fake(e, "comp.active"), fake(e, "blocks")))
        ok(rc == 0 and "view.removed" in fake(e, "view.removed") and fake(e, "ctrl.terminate") and fake(e, "comp.terminate"),
           "1l. `q` : vue retirée, contrôleur et composant terminés, sortie 0", (rc, e[-6:]))
        ok(not any("BAD" in l for l in e), "1m. blocs bien formés (32 échantillons, bus stéréo)", [l for l in e if "BAD" in l])
        ok(not fake(e, "comp.state BEFORE") and [l for l in e if l.startswith(("FAKE proc.setup", "FAKE comp.state"))][:2]
           == ["FAKE proc.setup 32 48000", "FAKE comp.state <COMP>"],
           "1o. setupProcessing AVANT l'état de la console, comme la console (LSP divise par sa fréquence dès l'état "
           "reçu : SIGFPE avant), activé ensuite", [l for l in e if "setup" in l or "comp.state" in l or "active" in l])

        # ---------------- catalogue (list-file) : pas de vue SANS affichage n'est pas un verdict
        def listing(display, mode):
            r = subprocess.run([exe, "list", str(display), mode, os.path.join(TMP, "vst3")], capture_output=True,
                               text=True, env=env, timeout=30)
            return [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        cat = {(d, m): listing(d, m) for d in (0, 1) for m in ("usual", "needs_display", "none")}
        reasons = {k: [(j["usable"], j["reason"]) for j in v] for k, v in cat.items()}
        ok(reasons[(1, "needs_display")] == [(True, "")] and reasons[(0, "needs_display")] == [(False, "no_display")],
           "1p1. éditeur construit dans createView avec l'affichage (LSP) : utilisable avec un affichage ; sans, "
           "`no_display` — jamais `no_ui`", reasons)
        ok(reasons[(1, "none")] == [(False, "no_ui")] and reasons[(1, "usual")] == [(True, "")]
           and reasons[(0, "usual")] == [(True, "")],
           "1p2. un plugin VRAIMENT sans éditeur reste `no_ui` (avec affichage) ; un éditeur ordinaire, utilisable "
           "avec ou sans", reasons)
        many = listing(1, "many")
        ok(len(many) == 198 and len(set(j["uri"] for j in many)) == 198 and all(j["usable"] for j in many),
           "1p3. paquet de 396 classes (LSP 1.2.35 : 198 effets + leurs contrôleurs) : les 198 effets listés (la borne "
           "de 256 classes en coupait 70)", (len(many), many[-1:] if many else None))

        # ---------------- VST3 sans état : les valeurs de la console, type d'abord
        h = Host(exe, ["vst3"], env)
        h.send("d p10 -60 10 0 -11", "d p20 0 3 4 2", "d p30 0 2 4 0", "s -")
        h.until(lambda l: l.startswith("ready "))
        h.finish()
        sets = fake(h.err, "ctrl.set")
        ok(sets[:2] == ["ctrl.set 20 0.6667", "ctrl.set 10 0.7000"] and not any(s.startswith("ctrl.set 30") for s in sets),
           "1a2. sans état : les valeurs de la console posées (type d'abord), seulement celles qui diffèrent", sets)

        # ---------------- CLAP actif
        cstate = b"bobistudio-clap-state-1\n--values 7\np1=0.5\n--clap 4\nBLOB"
        h = Host(exe, ["clap"], env)
        h.send("d p1 0 1 0 0.5", "d p2 0 10 4 3", "d p3 0 1 0 0", "d p4 0 2 0 0.2", "s " + base64.b64encode(cstate).decode())
        ready = h.until(lambda l: l.startswith("ready "))
        ok(ready == "ready 300 200" and "geom 300 200 resize 1" in h.out,
           "1h3. CLAP : taille de get_size, can_resize faux -> fixed 1", (ready, h.out))
        h.until(lambda l: l == "w p2 7")
        h.until(lambda l: l.startswith("geom 320"), 1.0)
        h.send("v p1 0.25")
        time.sleep(0.15)
        h.send("v p1 0.75")
        time.sleep(0.2)
        h.send("v p4 1")
        time.sleep(0.2)
        h.until(lambda l: l == "never", 0.2)
        rc = h.finish()
        e = h.err
        cin = fake(e, "clap.in")
        ok(fake(e, "clap.state") == ["clap.state <BLOB>"], "1a3. CLAP : le blob clap.state de la console, tel quel", fake(e, "clap.state"))
        ok("w p1 0.25" in h.out and "geom 320 240 resize 1" in h.out and h.out.index("w p1 0.25") < h.out.index("geom 320 240 resize 1")
           and "clap.out 1 via process" in fake(e, "clap.out"),
           "1j2. geste de la GUI -> sorti par process() (DPF ne le rend que là) -> valeur console", (h.out, fake(e, "clap.out")))
        ok("geom 320 240 resize 1" in h.out, "1h4. request_resize -> geom 320 x 240 « resize »", h.out)
        ok("w p2 7" in h.out, "1f2. rescan(VALUES) : ce que le plugin a changé de lui-même part", h.out)
        ok("clap.in 1 0.7500 via process" in cin and not any(s.startswith("clap.in 1 0.2500") for s in cin),
           "1c2. CLAP : valeur de la console -> événement du bloc ; celle que la GUI a postée n'est pas reposée", cin)
        ok(not any(s.startswith("clap.in 4") for s in cin) and not any(l.startswith("w p4") for l in h.out)
           and not any(l.startswith("w p3") for l in h.out),
           "1g3. CLAP : bornes différentes -> relayé dans aucun sens ; bypass non exposé", (cin, h.out))
        ok(fake(e, "audio thread during process 1"), "1j3. thread-check : fil audio pendant process(), et seulement là", e[-4:])
        ok(not any("NOISE" in l for l in h.out) and rc == 0 and fake(e, "clap.deactivate") and fake(e, "clap.destroy"),
           "1i2. CLAP : bruit du plugin hors protocole ; fermeture : GUI détruite, désactivé, détruit", (h.out, e[-5:]))
        ok(fake(e, "clap_entry.init") == ["clap_entry.init %s" % os.path.join(TMP, "clap", "sub", "Fake.clap")],
           "1n. URI clap:<sous-dossier>/<fichier>#id résolue sous CLAP_PATH", fake(e, "clap_entry.init"))

        # ---------------- CLAP qui refuse activate() : params.flush
        h = Host(exe, ["clap", "inactive"], env)
        h.send("d p1 0 1 0 0.5", "d p2 0 10 4 3", "s -")
        h.until(lambda l: l.startswith("ready "))
        h.until(lambda l: l.startswith("geom 320"))
        h.send("v p1 0.9")
        time.sleep(0.3)
        h.finish()
        e = h.err
        ok("w p1 0.25" in h.out and "geom 320 240 resize 1" in h.out and h.out.index("w p1 0.25") < h.out.index("geom 320 240 resize 1")
           and "clap.out 1 via flush" in fake(e, "clap.out") and "clap.in 1 0.9000 via flush" in fake(e, "clap.in")
           and not fake(e, "clap.flush OFF"),
           "1j4. CLAP inactif : gestes et valeurs par params.flush(), sur le fil principal", (h.out, fake(e, "clap.")))

# ====================================================================== 2. service
sys.path.insert(0, os.path.join(GUI, "src"))
import link as lk      # noqa: E402
import service as sv   # noqa: E402

params = [{"symbol": "p1", "min": 0, "max": 1, "flags": 0}, {"symbol": "p2", "min": 0, "max": 3, "flags": 4},
          {"symbol": "bad sym", "min": 0, "max": 1}, {"symbol": "p9", "min": "x", "max": 1},
          {"symbol": "p8", "min": 0, "max": float("inf")}, {"symbol": "p7", "min": 0, "max": 1, "flags": 1 | 64}]
b64 = base64.b64encode(b"state").decode()
pre = sv.preamble(params, {"p1": 0.25, "p2": 2, "p7": 0.5}, b64)
ok(pre == ["d p1 0.25 1 0 0.25"[:0] + "d p1 0 1 0 0.25", "d p2 0 3 4 2", "d p7 0 1 1 0.5", "s " + b64],
   "2a. préambule : un `d` par paramètre (bornes, drapeaux de la console, valeur), le reste écarté ; `s` base64 en dernier", pre)
ok(sv.preamble(params[:1], {}, "not base64!")[-1] == "s -" and sv.preamble([], {}, "A" * (sv.STATE_B64_MAX + 4))[-1] == "s -"
   and sv.preamble([], {}, "")[-1] == "s -", "2b. état absent, malformé ou trop gros : `s -` (valeurs seules)")
ok(sv.is_native("vst3:A.vst3#00") and sv.is_native("clap:a.clap#x") and not sv.is_native("http://lv2/x")
   and not sv.is_native("urn:zamaudio:ZamComp"), "2c. VST3 / CLAP reconnus à leur URI, le reste est LV2")
lst = "\n".join(['{"uri":"vst3:A.vst3#0F","name":"A","usable":true,"external":false}',
                 '{"uri":"clap:B.clap#b","name":"B","usable":true}', 'garbage', '[1]',
                 '{"uri":"vst3:A.vst3#1F","usable":false,"reason":"no_ui"}'])
pl, un = sv.parse_listing(lst, "vst3")
ok(pl == {"vst3:A.vst3#0F": {"name": "A", "external": False, "format": "vst3"}} and un == {"vst3:A.vst3#1F": {"reason": "no_ui", "missing": None}},
   "2d. liste d'un fichier VST3 : une URI d'un autre format est refusée, les inutilisables gardent leur raison", (pl, un))
lib = os.path.join(TMP, "lib")
os.makedirs(os.path.join(lib, "clap", "Vendor"))
os.makedirs(os.path.join(lib, "vst3", "X.vst3", "Contents", "x86_64-linux"))
open(os.path.join(lib, "clap", "Vendor", "a.clap"), "w").close()
open(os.path.join(lib, "clap", ".hidden.clap"), "w").close()
open(os.path.join(lib, "vst3", "X.vst3", "Contents", "x86_64-linux", "X.so"), "w").write("1")
roots = ((os.path.join(lib, "clap"), ".clap"), (os.path.join(lib, "vst3"), ".vst3"))
f1 = sv.library_files(roots)
ok(sorted(r for _, r, _ in f1) == ["Vendor/a.clap", "X.vst3"], "2e. bibliothèque : fichiers CLAP (dossier d'éditeur compris) et paquets VST3, cachés ignorés", f1)
time.sleep(0.01)
open(os.path.join(lib, "vst3", "X.vst3", "Contents", "x86_64-linux", "X.so"), "w").write("22")
f2 = sv.library_files(roots)
ok(dict((r, s) for _, r, s in f1)["X.vst3"] != dict((r, s) for _, r, s in f2)["X.vst3"]
   and dict((r, s) for _, r, s in f1)["Vendor/a.clap"] == dict((r, s) for _, r, s in f2)["Vendor/a.clap"],
   "2f. un binaire remplacé dans un paquet change sa signature (relu), les autres non", (f1, f2))


# -- catalogue : un paquet VST3 listé sur l'affichage du catalogue ; `no_display` jamais gardé ; jamais bloquant
class FakeDisplay:
    def __init__(self):
        self.works, self.names, self.stops = True, 0, 0

    def name(self):
        self.names += 1
        return ":19" if self.works else ""

    def stop(self):
        self.stops += 1


class FakeRun:
    def __init__(self):
        self.calls, self.during = [], []
        self.cat = None

    def __call__(self, args, capture_output=True, timeout=None, env=None):
        env = env or {}
        self.calls.append((args[1], args[-1], env.get("DISPLAY"), timeout))

        class R:
            returncode = 0
            stdout = b""
        if args[1] == "list":
            R.stdout = b'{"uri":"urn:lv2x","name":"L","usable":true}\n'
        elif args[-1].endswith(".clap"):
            R.stdout = b'{"uri":"clap:Vendor/a.clap#a","name":"A","usable":true}\n'
        else:
            self.during.append(dict(self.cat.data["plugins"]))
            line = {"uri": "vst3:X.vst3#00", "name": "X", "usable": bool(env.get("DISPLAY")),
                    "reason": "" if env.get("DISPLAY") else "no_display"}
            R.stdout = (json.dumps(line) + "\n").encode()
        return R


saved = (sv.LV2_PATH, sv.CLAP_PATH, sv.VST3_PATH)
os.makedirs(os.path.join(lib, "lv2"))
sv.LV2_PATH, sv.CLAP_PATH, sv.VST3_PATH = os.path.join(lib, "lv2"), os.path.join(lib, "clap"), os.path.join(lib, "vst3")
try:
    run, disp = FakeRun(), FakeDisplay()
    cat = sv.Catalog(run=run, display=disp)
    run.cat = cat
    disp.works = False
    d1 = cat.get()
    v3calls = [c for c in run.calls if c[1] == "X.vst3"]
    ok(v3calls and v3calls[-1][2] == "" and d1["unusable"].get("vst3:X.vst3#00", {}).get("reason") == "no_display"
       and ("vst3:X.vst3#00" not in [u for u in d1["plugins"]]) and (sv.VST3_PATH, "X.vst3") not in cat.files
       and cat.mtime is None,
       "2n1. affichage indisponible : `no_display`, NI gardé dans le cache NI pris pour un verdict (relu au prochain "
       "regard)", (v3calls, d1, list(cat.files)))
    disp.works = True
    cat.t = 0.0
    d2 = cat.get()
    v3calls = [c for c in run.calls if c[1] == "X.vst3"]
    clapcalls = [c for c in run.calls if c[1].endswith(".clap")]
    ok("vst3:X.vst3#00" in d2["plugins"] and v3calls[-1][2] == ":19" and v3calls[-1][3] >= 300
       and clapcalls and all(c[2] == "" for c in clapcalls) and (sv.VST3_PATH, "X.vst3") in cat.files,
       "2n2. paquet VST3 listé sur l'affichage du catalogue (:19, délai >= 300 s : LSP construit ~200 éditeurs), CLAP "
       "sans affichage ; verdict gardé", (v3calls, clapcalls, d2))
    ok(disp.stops >= 2 and disp.names >= 2, "2n3. l'affichage du catalogue est arrêté à la fin de chaque balayage",
       (disp.names, disp.stops))
    run2 = FakeRun()
    cat2 = sv.Catalog(run=run2, display=FakeDisplay())       # a fresh catalog (nothing published) whose cache knows
    run2.cat = cat2                                         # the CLAP file, not the VST3 bundle
    cat2.files = {k: v for k, v in cat.files.items() if k[1] != "X.vst3"}
    cat2.get()
    ok(run2.during and "urn:lv2x" in run2.during[-1] and "clap:Vendor/a.clap#a" in run2.during[-1]
       and not [c for c in run2.calls if c[1].endswith(".clap")],
       "2n4. pendant qu'un paquet est listé, le catalogue publie déjà LV2 et ce que le cache connaît", run2.during)
    n = len(run.calls)
    cat.t = 0.0
    cat.mtime = None
    cat.get()
    ok(not [c for c in run.calls[n:] if c[1] in ("X.vst3", "Vendor/a.clap")],
       "2n5. relecture sans changement de fichier : rien n'est relisté (cache par signature)", run.calls[n:])
    cat.lock.acquire()
    t0 = time.monotonic()
    cat.t, cat.mtime = 0.0, None
    d3 = cat.get()
    cat.lock.release()
    ok(time.monotonic() - t0 < 0.5 and d3 is cat.data and "vst3:X.vst3#00" in d3["plugins"],
       "2n6. un balayage en cours (verrou pris) ne bloque pas /catalog : ce qui est connu est rendu",
       time.monotonic() - t0)
finally:
    sv.LV2_PATH, sv.CLAP_PATH, sv.VST3_PATH = saved


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body=None, **kw):
        self.calls.append((method, url.split(":8082", 1)[-1], body))
        if "/plugins/params" in url and "values=1" not in url:
            return 200, {"uri": self.uri, "params": [{"symbol": "p1", "min": 0, "max": 1, "flags": 0, "output": False},
                                                     {"symbol": "p2", "min": 0, "max": 1, "flags": 1, "output": True}],
                         "values": {"p1": 0.5, "p2": 0.1}}
        if url.endswith("/plugins/save_state"):
            return 200, {"ok": True, "state": b64}
        return 200, {"values": {}}


class S:
    console, console_ip, element_id, element_path, slot, tag, user = "c1", "10.0.0.1", "m1", "strips.s1.chain.m1", 0, "0123456789ab", "u"


def link_for(uri, http, pushed):
    return lk.Link("10.0.0.1", "m1", "strips.s1.chain.m1", uri, push=lambda k, v: pushed.append((k, v)), ended=lambda r: None,
                   http=http)


L = sv.Launcher({})
for uri in ("vst3:A.vst3#00", "clap:B.clap#b"):
    s = S()
    s.uri = uri
    http, pushed = FakeHttp(), []
    http.uri = uri
    rt = sv.Runtime()
    rt.link = link_for(uri, http, pushed)
    values, lines = L._console_view(s, rt)
    L._link_up(s, rt, values)
    ok(lines == ["d p1 0 1 0 0.5", "d p2 0 1 1 0.1", "s " + b64] and [c[1] for c in http.calls] == ["/plugins/params?id=m1",
                                                                                                  "/plugins/save_state"]
       and http.calls[1][2] == {"id": "m1"},
       "2g. %s : vue de la console (paramètres, valeurs, état) lue AVANT de créer le plugin -> préambule" % uri.split(":")[0],
       (lines, http.calls))
    ok(pushed == [] and rt.atoms is None and rt.link.sent == {"p1": 0.5, "p2": 0.1},
       "2h. %s : rien de poussé à l'ouverture (l'hôte tient déjà tout), pas d'atomes" % uri.split(":")[0], (pushed, rt.atoms))
    rt.link.apply_values({"p1": 0.5, "p2": 0.2})
    ok(pushed == [("p2", 0.2)], "2i. %s : ensuite, seul ce qui change est poussé" % uri.split(":")[0], pushed)
s = S()
s.uri = "urn:zamaudio:ZamComp"
http, pushed = FakeHttp(), []
http.uri = s.uri
rt = sv.Runtime()
rt.link = link_for(s.uri, http, pushed)
values, lines = L._console_view(s, rt)
ok(values is None and lines == [] and http.calls == [], "2j. LV2 : aucune lecture avant la création (comme avant 0.1.4)", http.calls)
L._link_up(s, rt, values)
ok(sorted(pushed) == [("p1", 0.5), ("p2", 0.1)] and isinstance(rt.atoms, lk.AtomLink)
   and not any(c[1].endswith("save_state") for c in http.calls),
   "2k. LV2 : tous les ports poussés une fois, atomes branchés, aucun état demandé (chemin inchangé)", (pushed, rt.atoms))
http2 = FakeHttp()
http2.uri = "vst3:x"
logs = []
lk2 = lk.Link("10.0.0.1", "m1", "p", "vst3:x", push=None, ended=None, http=lambda *a, **k: (409, {"error": "state_unavailable"}),
              log=logs.append)
ok(lk2.fetch_state() == "" and logs, "2l. état refusé par la console : vide (l'interface part des valeurs), et dit", logs)
src = open(os.path.join(GUI, "src", "service.py"), encoding="utf-8").read()
start = src[src.index("    def start(self, s):"):src.index("    def _console_view")]
ok(start.index("self._console_view(s, rt)") < start.index('"run", s.uri') < start.index("for line in lines")
   < start.index("self._wait_ready("), "2m. ordre : vue de la console, puis le plugin, puis le préambule écrit, puis `ready` attendu")

print("\n%d échec(s)" % len(ko) if ko else "\nTout est vert.")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if ko else 0)
