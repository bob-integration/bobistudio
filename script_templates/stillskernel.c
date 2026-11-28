/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * Noyau du plugin Images fixes (libbobi_stills) : RGBA 8 bits → trame planar Fill (Y, U, V) et Key,
 * en UNE passe, sur plusieurs cœurs (2026-11-15).
 *
 * Pourquoi : préparer une image 1080p coûtait 89 ms de conversion RGB → YUV en numpy (passes
 * flottantes successives), dans le processus qui publie la sortie. Ici : quelques ms.
 *
 * Contrat : le MÊME calcul que plugins/stills/script.py (_rgb_to_yuv420 / _alpha_to_yuv420), qui
 * reste la référence et le repli :
 *   - matrice BT.709, plage STUDIO (Y 16-235, C 16-240), en float32, termes additionnés dans
 *     l'ordre de l'expression Python ; compilé -ffp-contract=off (pas de FMA : mêmes arrondis) ;
 *   - chrominance sous-échantillonnée par MOYENNE des pixels regroupés (cw × ch), pas un sur deux ;
 *   - mise à l'échelle de la profondeur (× scale), écrêtage [0, maxv], troncature ;
 *   - Key : Y = alpha × scale (écrêté), U = V = neutre.
 * Entrée RGBA interleavée contiguë (H, W, 4) uint8 ; sorties planes contiguës, uint8 ou uint16
 * selon la profondeur. w % cw == 0, h % ch == 0, sinon -1 (l'appelant garde son repli).
 */
#include <stdint.h>
#include <stddef.h>

#define EXPORT __attribute__((visibility("default")))
#define PAR_MIN (256 * 256)

static inline float clampf(float x, float maxv) { return x < 0.0f ? 0.0f : (x > maxv ? maxv : x); }

#define FILL_BODY(OUT_T)                                                                           \
    _Pragma("omp parallel for schedule(static) if((int64_t)h * w >= PAR_MIN)")                    \
    for (int by = 0; by < h / ch; by++) {                                                          \
        for (int sub = 0; sub < ch; sub++) {                                                       \
            const int r = by * ch + sub;                                                           \
            const uint8_t *px = rgba + (size_t)r * w * 4;                                          \
            OUT_T *yr = y + (size_t)r * w;                                                         \
            for (int c = 0; c < w; c++) {                                                          \
                const float R = (float)px[4 * c], G = (float)px[4 * c + 1], B = (float)px[4 * c + 2]; \
                const float fy = 0.18259f * R + 0.61423f * G + 0.06201f * B + 16.0f;               \
                yr[c] = (OUT_T)clampf(fy * fscale, fmaxv);                                         \
            }                                                                                      \
        }                                                                                          \
        OUT_T *ur = u + (size_t)by * (w / cw);                                                     \
        OUT_T *vr = v + (size_t)by * (w / cw);                                                     \
        for (int c = 0; c < w / cw; c++) {                                                         \
            float su = 0.0f, sv = 0.0f;                                                            \
            for (int sub = 0; sub < ch; sub++) {                                                   \
                const uint8_t *px = rgba + ((size_t)(by * ch + sub) * w + (size_t)c * cw) * 4;     \
                for (int k = 0; k < cw; k++) {                                                     \
                    const float R = (float)px[4 * k], G = (float)px[4 * k + 1], B = (float)px[4 * k + 2]; \
                    su += -0.10064f * R - 0.33857f * G + 0.43922f * B + 128.0f;                    \
                    sv +=  0.43922f * R - 0.39894f * G - 0.04027f * B + 128.0f;                    \
                }                                                                                  \
            }                                                                                      \
            const float n = (float)(cw * ch);                                                      \
            ur[c] = (OUT_T)clampf((su / n) * fscale, fmaxv);                                       \
            vr[c] = (OUT_T)clampf((sv / n) * fscale, fmaxv);                                       \
        }                                                                                          \
    }

/* ABI 2 (2026-11-15) : bobi_stills_mixd_u8/u16 — dithered transition mix. */
EXPORT int bobi_stills_abi(void) { return 2; }

/* Dithered mix for the dissolve: dst = clamp(trunc(a·fa + b·fb + t), 0, maxv), where t ∈ [0, 1)
 * is a fixed per-pixel threshold read from a 64×64 tile. Why: between two similar images a pixel
 * moves by only a few code values over the whole fade, so in 8 bits it steps 0, 1 or 2 codes per
 * frame — and pixels with similar differences all step on the SAME frame, which the eye reads as
 * a jerky fade (measured on a tester's recording, 2026-11-15). A per-pixel threshold spreads those
 * steps over time. Ends stay exact: a and b are integers and t < 1, so w = 0 gives a, w = 1 gives b.
 * Always clamped: fa + fb may round above 1 in float. 2D arrays, last axis contiguous, strides in
 * elements; `tile` is 64×64 float32, indexed by (row % 64, col % 64). */
#define MIXD_BODY(T)                                                                       \
    _Pragma("omp parallel for schedule(static) if((int64_t)h * w >= PAR_MIN)")             \
    for (int r = 0; r < h; r++) {                                                          \
        T *d = dst + (size_t)r * dst_stride;                                               \
        const T *pa = a + (size_t)r * a_stride;                                            \
        const T *pb = b + (size_t)r * b_stride;                                            \
        const float *tr = tile + (size_t)(r & 63) * 64;                                    \
        for (int c = 0; c < w; c++) {                                                      \
            float v = (float)pa[c] * fa + (float)pb[c] * fb + tr[c & 63];                  \
            d[c] = (T)(v < 0.0f ? 0.0f : (v > fmaxv ? fmaxv : v));                         \
        }                                                                                  \
    }

EXPORT void bobi_stills_mixd_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                                const uint8_t *a, ptrdiff_t a_stride,
                                const uint8_t *b, ptrdiff_t b_stride,
                                int h, int w, double coef_a, double coef_b,
                                const float *restrict tile, int maxv)
{
    const float fa = (float)coef_a, fb = (float)coef_b, fmaxv = (float)maxv;
    MIXD_BODY(uint8_t)
}

EXPORT void bobi_stills_mixd_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                                 const uint16_t *a, ptrdiff_t a_stride,
                                 const uint16_t *b, ptrdiff_t b_stride,
                                 int h, int w, double coef_a, double coef_b,
                                 const float *restrict tile, int maxv)
{
    const float fa = (float)coef_a, fb = (float)coef_b, fmaxv = (float)maxv;
    MIXD_BODY(uint16_t)
}

EXPORT int bobi_stills_fill_u8(const uint8_t *restrict rgba, int h, int w, uint8_t *restrict y,
                               uint8_t *restrict u, uint8_t *restrict v, int cw, int ch,
                               int scale, int maxv)
{
    if (cw < 1 || ch < 1 || w % cw || h % ch) return -1;
    const float fscale = (float)scale, fmaxv = (float)maxv;
    FILL_BODY(uint8_t)
    return 0;
}

EXPORT int bobi_stills_fill_u16(const uint8_t *restrict rgba, int h, int w, uint16_t *restrict y,
                                uint16_t *restrict u, uint16_t *restrict v, int cw, int ch,
                                int scale, int maxv)
{
    if (cw < 1 || ch < 1 || w % cw || h % ch) return -1;
    const float fscale = (float)scale, fmaxv = (float)maxv;
    FILL_BODY(uint16_t)
    return 0;
}

/* Key : Y = alpha × scale écrêté ; U et V = neutre (n_uv échantillons chacun). */
EXPORT int bobi_stills_key_u16(const uint8_t *restrict rgba, int h, int w, uint16_t *restrict y,
                               uint16_t *restrict uv, int n_uv, int scale, int maxv, int neutral)
{
    _Pragma("omp parallel for schedule(static) if((int64_t)h * w >= PAR_MIN)")
    for (int r = 0; r < h; r++) {
        const uint8_t *px = rgba + (size_t)r * w * 4;
        uint16_t *yr = y + (size_t)r * w;
        for (int c = 0; c < w; c++) {
            const int k = px[4 * c + 3] * scale;
            yr[c] = (uint16_t)(k > maxv ? maxv : k);
        }
    }
    for (int i = 0; i < 2 * n_uv; i++) uv[i] = (uint16_t)neutral;
    return 0;
}

EXPORT int bobi_stills_key_u8(const uint8_t *restrict rgba, int h, int w, uint8_t *restrict y,
                              uint8_t *restrict uv, int n_uv, int scale, int maxv, int neutral)
{
    (void)scale; (void)maxv;
    _Pragma("omp parallel for schedule(static) if((int64_t)h * w >= PAR_MIN)")
    for (int r = 0; r < h; r++) {
        const uint8_t *px = rgba + (size_t)r * w * 4;
        uint8_t *yr = y + (size_t)r * w;
        for (int c = 0; c < w; c++) yr[c] = px[4 * c + 3];
    }
    for (int i = 0; i < 2 * n_uv; i++) uv[i] = (uint8_t)neutral;
    return 0;
}
