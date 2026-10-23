/*
 * FAUSSE libmxl pour tests/verif_console_mxl_rt.py (le contrôleur n'a pas libmxl).
 *
 * Expose les MÊMES symboles et signatures que la vraie (mxl.h / flow.h de la 1.1.x) :
 *   mxlFlowReaderGetSamplesNonBlocking(reader, index, count, mxlWrappedMultiBufferSlice*)
 *   mxlFlowWriterOpenSamples(writer, index, count, mxlMutableWrappedMultiBufferSlice*)
 *   mxlFlowWriterCommitSamples(writer) / mxlFlowWriterCancelSamples(writer)
 *   mxlGetTime(), mxlGetVersion(mxlVersionType*)
 * et la même sémantique d'index : ONE-PAST-THE-END (l'API rend/écrit [index - count, index)), anneau
 * par canal (plans de `cap` échantillons, `stride` octets entre canaux, 2 fragments au tour d'anneau),
 * lecture NON bloquante qui refuse un index trop vieux (TOO_LATE : écrasé) ou trop neuf (TOO_EARLY :
 * au-delà de la tête).
 *
 * Ce que cette fausse ne reproduit PAS : les garde-fous internes de libmxl (génération, flux remplacé
 * pendant la lecture, FLOW_INVALID spontané), la tête partagée entre processus, la fenêtre lisible exacte
 * (ici cap - margin), les accès mémoire de libmxl lui-même (donc pas de faute DANS la bibliothèque), les
 * écritures concurrentes d'un producteur pendant la copie.
 *
 * Aides fake_* : création, remplissage, relecture, injection de statuts d'erreur, troncature du mapping
 * (vraie faute SIGBUS à l'accès), version, compteurs d'appels, drapeau « libéré » (un appel sur un flux
 * libéré est compté : preuve de la quiescence).
 */
#define _GNU_SOURCE
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

typedef struct { void *pointer; size_t size; } buf_t;
typedef struct { buf_t fragments[2]; } wrapped_t;
typedef struct { wrapped_t base; size_t stride; size_t count; } multi_t;
typedef struct { uint16_t major, minor, bugfix, build; const char *full; } version_t;

enum { OK = 0, ERR_UNKNOWN = 1, ERR_TOO_LATE = 3, ERR_TOO_EARLY = 4, ERR_INVALID_ARG = 8 };

typedef struct {
    int fd;
    char *map;
    size_t map_len, cap, stride;
    int ch;
    uint64_t margin;
    uint64_t head;
    int has_head;
    int fail_get, fail_open, fail_commit;     /* statut forcé (0 = aucun) */
    long open_countdown;                      /* >0 : l'ouverture numéro N renvoie fail_open_once puis revient à la normale */
    int fail_open_once;
    int pending;
    volatile long n_get, n_open, n_commit, n_cancel, after_free;
    uint64_t last_get_idx, last_get_cnt, last_open_idx, last_open_cnt;
    volatile int freed;
    long delay_ns;                            /* attente DANS l'appel de lecture : élargit la fenêtre d'une course */
    int touch_map;                            /* comme la vraie libmxl : l'appel lit la mémoire MAPPÉE (sa tête
                                                 d'anneau y vit) — un mapping tronqué faute DANS l'appel */
} flow_t;

static int g_vmaj = 1, g_vmin = 1, g_vbug = 0;

void *fake_create(int ch, size_t cap, uint64_t margin)
{
    flow_t *f = calloc(1, sizeof *f);
    if (!f) return NULL;
    f->ch = ch;
    f->cap = cap;
    f->stride = cap * 4;
    f->map_len = f->stride * (size_t)ch;
    f->margin = margin;
    f->fd = memfd_create("fake_mxl", 0);
    if (f->fd < 0 || ftruncate(f->fd, (off_t)f->map_len) != 0) { free(f); return NULL; }
    f->map = mmap(NULL, f->map_len, PROT_READ | PROT_WRITE, MAP_SHARED, f->fd, 0);
    if (f->map == MAP_FAILED) { close(f->fd); free(f); return NULL; }
    return f;
}

void fake_destroy(void *h)
{
    flow_t *f = h;
    munmap(f->map, f->map_len);
    close(f->fd);
    free(f);
}

void fake_mark_freed(void *h, int v) { ((flow_t *)h)->freed = v; }
void fake_set_delay(void *h, long ns) { ((flow_t *)h)->delay_ns = ns; }
void fake_set_touch(void *h, int on) { ((flow_t *)h)->touch_map = on; }
static void touch(flow_t *f) { if (f->touch_map) { volatile char x = f->map[0]; (void)x; } }

/* Troncature du fichier sous le mapping : tout accès au-delà de keep_bytes lève SIGBUS (vrai défaut). */
int fake_truncate(void *h, size_t keep_bytes) { flow_t *f = h; return ftruncate(f->fd, (off_t)keep_bytes); }

void fake_set_fail(void *h, int which, int status)
{
    flow_t *f = h;
    if (which == 0) f->fail_get = status;
    else if (which == 1) f->fail_open = status;
    else f->fail_commit = status;
}
void fake_fail_open_nth(void *h, long nth, int status) { flow_t *f = h; f->open_countdown = nth; f->fail_open_once = status; }

void fake_set_version(int maj, int min, int bug) { g_vmaj = maj; g_vmin = min; g_vbug = bug; }

long fake_counter(void *h, int which)
{
    flow_t *f = h;
    switch (which) {
    case 0: return f->n_get;
    case 1: return f->n_open;
    case 2: return f->n_commit;
    case 3: return f->n_cancel;
    case 4: return f->after_free;
    case 5: return (long)f->last_get_idx;
    case 6: return (long)f->last_get_cnt;
    case 7: return (long)f->last_open_idx;
    case 8: return (long)f->last_open_cnt;
    case 9: return (long)f->head;
    default: return -1;
    }
}

/* Écrit planar (ch × n, ligne = canal) aux index [start, start+n) et avance la tête (hors API : amorçage du test). */
void fake_put(void *h, uint64_t start, const float *planar, size_t n)
{
    flow_t *f = h;
    for (int c = 0; c < f->ch; c++)
        for (size_t i = 0; i < n; i++)
            memcpy(f->map + (size_t)c * f->stride + ((start + i) % f->cap) * 4, planar + (size_t)c * n + i, 4);
    if (!f->has_head || start + n > f->head) { f->head = start + n; f->has_head = 1; }
}

/* Relit planar (ch × n) aux index [start, start+n), sans passer par l'API. */
void fake_get(void *h, uint64_t start, size_t n, float *out)
{
    flow_t *f = h;
    for (int c = 0; c < f->ch; c++)
        for (size_t i = 0; i < n; i++)
            memcpy(out + (size_t)c * n + i, f->map + (size_t)c * f->stride + ((start + i) % f->cap) * 4, 4);
}

static void slices(flow_t *f, uint64_t first, size_t count, multi_t *out)
{
    size_t off = (size_t)(first % f->cap);
    size_t n0 = count < f->cap - off ? count : f->cap - off;
    memset(out, 0, sizeof *out);
    out->base.fragments[0].pointer = f->map + off * 4;
    out->base.fragments[0].size = n0 * 4;
    if (count > n0) {
        out->base.fragments[1].pointer = f->map;
        out->base.fragments[1].size = (count - n0) * 4;
    }
    out->stride = f->stride;
    out->count = (size_t)f->ch;
}

int mxlFlowReaderGetSamplesNonBlocking(void *reader, uint64_t index, size_t count, multi_t *out)
{
    flow_t *f = reader;
    if (f->freed) f->after_free++;
    f->n_get++;
    f->last_get_idx = index;
    f->last_get_cnt = count;
    if (f->delay_ns) {
        struct timespec a, b;
        clock_gettime(CLOCK_MONOTONIC, &a);
        do clock_gettime(CLOCK_MONOTONIC, &b);
        while ((b.tv_sec - a.tv_sec) * 1000000000L + (b.tv_nsec - a.tv_nsec) < f->delay_ns);
        if (f->freed) f->after_free++;        /* l'appel a fini sur un handle fermé entre-temps */
    }
    touch(f);
    if (f->fail_get) return f->fail_get;
    if (!out || !count || count > f->cap) return ERR_INVALID_ARG;
    if (!f->has_head || index > f->head) return ERR_TOO_EARLY;
    uint64_t first = index - count;
    uint64_t oldest = f->head > f->cap - f->margin ? f->head - (f->cap - f->margin) : 0;
    if (index < count || first < oldest) return ERR_TOO_LATE;
    slices(f, first, count, out);
    return OK;
}

int mxlFlowWriterOpenSamples(void *writer, uint64_t index, size_t count, multi_t *out)
{
    flow_t *f = writer;
    touch(f);
    if (f->freed) f->after_free++;
    f->n_open++;
    f->last_open_idx = index;
    f->last_open_cnt = count;
    if (f->open_countdown > 0 && --f->open_countdown == 0) return f->fail_open_once;
    if (f->fail_open) return f->fail_open;
    if (!out || !count || count > f->cap || index < count) return ERR_INVALID_ARG;
    slices(f, index - count, count, out);
    f->pending = 1;
    return OK;
}

int mxlFlowWriterCommitSamples(void *writer)
{
    flow_t *f = writer;
    if (f->freed) f->after_free++;
    f->n_commit++;
    if (f->fail_commit) return f->fail_commit;
    if (!f->pending) return ERR_INVALID_ARG;
    f->pending = 0;
    /* la tête avance à l'index ouvert : on le retrouve par le dernier Open */
    if (!f->has_head || f->last_open_idx > f->head) { f->head = f->last_open_idx; f->has_head = 1; }
    return OK;
}

int mxlFlowWriterCancelSamples(void *writer)
{
    flow_t *f = writer;
    if (f->freed) f->after_free++;
    f->n_cancel++;
    f->pending = 0;
    return OK;
}

uint64_t mxlGetTime(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

int mxlGetVersion(version_t *v)
{
    if (!v) return ERR_INVALID_ARG;
    v->major = (uint16_t)g_vmaj;
    v->minor = (uint16_t)g_vmin;
    v->bugfix = (uint16_t)g_vbug;
    v->build = 0;
    v->full = "fake";
    return OK;
}
