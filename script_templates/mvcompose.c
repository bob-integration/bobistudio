/*
 * mvcompose — passes de compositing multiview FUSIONNÉES (chemin CPU).
 *
 * Chantier « fusion numpy → C » (2026-07-12, cf. mémoire processing-fusion-and-ht-isolation) :
 * le compose CPU du multiview est memory-bound, chaque op numpy = une passe mémoire séparée
 * (banc dl360-1 : kernel représentatif 22,6 ms en numpy soigné vs 3,1 ms en C fusionné
 * mono-thread, budget 50p = 20 ms). Ce module fusionne chaque opération en UNE passe :
 *   - mvk_blend_*      : dst = (dst·(255−α) + src·α) / 255            (≈5 passes numpy → 1)
 *   - mvk_blend_pre_*  : dst = (dst·inv_a + src_a) / 255              (opérandes pré-calculés)
 *   - mvk_place_*      : gather nearest lignes/colonnes → écriture canvas (gather+assign → 1)
 *
 * BIT-EXACT par construction avec les versions numpy de plugins/multiview/script.py :
 * même arithmétique ENTIÈRE (accumulation uint32, division entière par 255), et les INDEX
 * nearest sont calculés CÔTÉ PYTHON (deux formules coexistent dans le script — troncature
 * float de resize_plane vs division entière du chemin tranche — le C ne choisit pas).
 * Le repli numpy (lib absente : vieilles images) reste le code d'origine, inchangé.
 *
 * Strides en ÉLÉMENTS (pas en octets) ; le dernier axe de chaque tableau doit être contigu
 * (garanti par les wrappers bobimxl, sinon repli numpy). Alpha toujours uint8 (0..255),
 * y compris en pipeline 10/12 bits (données uint16).
 *
 * V210-READY : la résolution de la ligne source est isolée (pointeur srow calculé en tête
 * de boucle ligne). Si le tout-v210 (R3) est acté après les bancs membw/capacité, un
 * décodage v210→scratch par ligne se branche À CET ENDROIT (cf. v210convert.c, unpack par
 * groupe de 6 px) sans toucher ni l'API ni l'échantillonnage colonne. Rien d'autre à prévoir.
 *
 * OpenMP : parallélisation par lignes, déterministe (lignes indépendantes) → les octets ne
 * dépendent PAS du nombre de threads. Nb de threads = mvk_set_threads() (posé par bobimxl :
 * env BOBI_MVK_THREADS, défaut = cœurs PHYSIQUES du cpuset — le cpuset core_pool est
 * HT-aware, 2 logiques/cœur). Les petites bboxes (horloges) restent mono-thread (clause if).
 *
 * Build (plugins/_compute_runtime/Dockerfile, étage mvk-builder — image build-once+push sur
 * nœuds hétérogènes, JAMAIS -march=native) :
 *   gcc -O3 -march=x86-64-v2 -fopenmp -shared -fPIC mvcompose.c -o libbobi_mvk.so
 *   gcc -O3 -march=x86-64-v3 -fopenmp -shared -fPIC mvcompose.c -o libbobi_mvk_v3.so
 */

#include <stdint.h>
#include <stddef.h>
#include <math.h>

#define EXPORT __attribute__((visibility("default")))

/* Seuil de parallélisation : sous ~32 k éléments (petites bboxes d'horloge/VU) le fork/join
 * OpenMP coûte plus que la passe elle-même → mono-thread. */
#define PAR_MIN 32768

static int g_threads = 1;

EXPORT void mvk_set_threads(int n) { g_threads = n < 1 ? 1 : n; }
EXPORT int  mvk_get_threads(void) { return g_threads; }
EXPORT int  mvk_abi_version(void) { return 7; }   /* 2=+rgba2yuv 3=+mixf/mixmap 4=+spl_* 5=+spl_gradient 6=+spl_blur2/rotmap/rotring 7=+spl_prefuse/outer */

/* --- blend : dst = (dst·(255−α) + src·α) / 255 --------------------------------------- */
/* Équivalent bit-exact de script.py blend() : accumulation uint32, division entière.     */

EXPORT void mvk_blend_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                         const uint8_t *restrict src, ptrdiff_t src_stride,
                         const uint8_t *restrict alpha, ptrdiff_t a_stride,
                         int h, int w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)
    for (int r = 0; r < h; r++) {
        uint8_t *d = dst + (size_t)r * dst_stride;
        const uint8_t *s = src + (size_t)r * src_stride;
        const uint8_t *a = alpha + (size_t)r * a_stride;
        for (int c = 0; c < w; c++) {
            uint32_t av = a[c];
            d[c] = (uint8_t)(((uint32_t)d[c] * (255u - av) + (uint32_t)s[c] * av) / 255u);
        }
    }
}

EXPORT void mvk_blend_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                          const uint16_t *restrict src, ptrdiff_t src_stride,
                          const uint8_t *restrict alpha, ptrdiff_t a_stride,
                          int h, int w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)
    for (int r = 0; r < h; r++) {
        uint16_t *d = dst + (size_t)r * dst_stride;
        const uint16_t *s = src + (size_t)r * src_stride;
        const uint8_t *a = alpha + (size_t)r * a_stride;
        for (int c = 0; c < w; c++) {
            uint32_t av = a[c];
            d[c] = (uint16_t)(((uint32_t)d[c] * (255u - av) + (uint32_t)s[c] * av) / 255u);
        }
    }
}

/* --- blend_pre : dst = (dst·inv_a + src_a) / 255 -------------------------------------- */
/* Équivalent bit-exact de script.py blend_pre() : opérandes pré-calculés au bake du chrome,
 * dtype _ACC = uint16 en 8 bits (somme ≤ 255·255, pas de débordement) / uint32 en 10/12 b.
 * L'accumulation ici est uint32 dans les deux cas → mêmes octets (valeurs ≤ 4095·255).     */

EXPORT void mvk_blend_pre_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                             const uint16_t *restrict inv_a, ptrdiff_t ia_stride,
                             const uint16_t *restrict src_a, ptrdiff_t sa_stride,
                             int h, int w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)
    for (int r = 0; r < h; r++) {
        uint8_t *d = dst + (size_t)r * dst_stride;
        const uint16_t *ia = inv_a + (size_t)r * ia_stride;
        const uint16_t *sa = src_a + (size_t)r * sa_stride;
        for (int c = 0; c < w; c++)
            d[c] = (uint8_t)(((uint32_t)d[c] * ia[c] + sa[c]) / 255u);
    }
}

EXPORT void mvk_blend_pre_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                              const uint32_t *restrict inv_a, ptrdiff_t ia_stride,
                              const uint32_t *restrict src_a, ptrdiff_t sa_stride,
                              int h, int w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)
    for (int r = 0; r < h; r++) {
        uint16_t *d = dst + (size_t)r * dst_stride;
        const uint32_t *ia = inv_a + (size_t)r * ia_stride;
        const uint32_t *sa = src_a + (size_t)r * sa_stride;
        for (int c = 0; c < w; c++)
            d[c] = (uint16_t)(((uint32_t)d[c] * ia[c] + sa[c]) / 255u);
    }
}

/* --- place : gather nearest → écriture canvas ------------------------------------------ */
/* dst[r][c] = src[row_idx[r]][col0 + c·col_step]   (col_step > 0 : décimation régulière,
 * vectorisable) ou src[row_idx[r]][col_idx[c]] (col_step ≤ 0 : gather générique, ratio non
 * entier). row_idx/col_idx = indices SOURCE absolus int32 calculés côté Python (formule du
 * call-site : resize_plane OU chemin tranche) → bit-exact. Remplace gather+assign (2 passes
 * mémoire + tableau intermédiaire) par une écriture directe (1 passe).
 * V210-READY : `srow` est le seul point qui suppose une source planar — un décodeur de
 * ligne (v210 → scratch) se branche ici le jour où R3 est acté. */

EXPORT void mvk_place_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                         const uint8_t *restrict src, ptrdiff_t src_stride,
                         const int32_t *restrict row_idx,
                         int64_t col0, int64_t col_step,
                         const int32_t *restrict col_idx,
                         int out_h, int out_w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)out_h * out_w >= PAR_MIN)
    for (int r = 0; r < out_h; r++) {
        uint8_t *d = dst + (size_t)r * dst_stride;
        const uint8_t *srow = src + (size_t)row_idx[r] * src_stride;   /* ← point V210 */
        if (col_step > 0) {
            const uint8_t *s = srow + col0;
            for (int c = 0; c < out_w; c++)
                d[c] = s[(size_t)c * col_step];
        } else {
            for (int c = 0; c < out_w; c++)
                d[c] = srow[col_idx[c]];
        }
    }
}

EXPORT void mvk_place_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                          const uint16_t *restrict src, ptrdiff_t src_stride,
                          const int32_t *restrict row_idx,
                          int64_t col0, int64_t col_step,
                          const int32_t *restrict col_idx,
                          int out_h, int out_w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)out_h * out_w >= PAR_MIN)
    for (int r = 0; r < out_h; r++) {
        uint16_t *d = dst + (size_t)r * dst_stride;
        const uint16_t *srow = src + (size_t)row_idx[r] * src_stride;  /* ← point V210 */
        if (col_step > 0) {
            const uint16_t *s = srow + col0;
            for (int c = 0; c < out_w; c++)
                d[c] = s[(size_t)c * col_step];
        } else {
            for (int c = 0; c < out_w; c++)
                d[c] = srow[col_idx[c]];
        }
    }
}

/* --- rgba2yuv : RGBA → Y + U/V sous-échantillonnés, FUSIONNÉ (ABI 2) -------------------- */
/* Équivalent bit-exact de rgba_to_yuv / _rgba_to_yuv_xp (multiview) : mêmes expressions
 * float32 DANS LE MÊME ORDRE — le fichier DOIT être compilé -ffp-contract=off (une
 * contraction FMA changerait les arrondis vs numpy), puis clip [0, maxv], cast tronqué.
 * Le sous-échantillonnage chroma réplique _sub_avg : moyennes PAR PAIRES séquentielles
 * ((a+b+1)//2 colonnes PUIS lignes) sur les valeurs ENTIÈRES pleine résolution — pas une
 * moyenne à 4. L'alpha reste côté Python (1 passe numpy triviale, dtypes divergents entre
 * les deux appelants historiques). Entrée : RGBA interleavé, uint8 (chemin PIL) ou float32
 * (chemin tuile xp) ; strides en PIXELS (1 px = 4 échantillons). w % cw == 0, h % ch == 0
 * (bboxes chroma-alignées — garanti par les wrappers bobimxl, sinon repli numpy). */

static inline void rgba2yuv_px(float r, float g, float b, float scale, float maxv,
                               float *restrict oy, float *restrict ou, float *restrict ov)
{
    float y = (0.299f * r + 0.587f * g + 0.114f * b) * scale;
    float u = (-0.169f * r - 0.331f * g + 0.500f * b + 128.0f) * scale;
    float v = (0.500f * r - 0.419f * g - 0.081f * b + 128.0f) * scale;
    *oy = y < 0.0f ? 0.0f : (y > maxv ? maxv : y);
    *ou = u < 0.0f ? 0.0f : (u > maxv ? maxv : u);
    *ov = v < 0.0f ? 0.0f : (v > maxv ? maxv : v);
}

/* Corps générique : IN_T = uint8_t|float, OUT_T = uint8_t|uint16_t. Une bande de `ch` lignes
 * par itération : Y écrit direct, chroma pleine résolution gardée en scratch entier puis
 * réduite par paires (ordre numpy : colonnes puis lignes). VLA scratch : 2 lignes × w u32. */
#define RGBA2YUV_BODY(IN_T, OUT_T)                                                        \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int by = 0; by < h / ch; by++) {                                                 \
        uint32_t cu[2][8192], cv[2][8192];                                                \
        for (int sub = 0; sub < ch; sub++) {                                              \
            int r = by * ch + sub;                                                        \
            const IN_T *px = rgba + (size_t)r * rgba_stride * 4;                          \
            OUT_T *yr = y + (size_t)r * y_stride;                                         \
            for (int c = 0; c < w; c++) {                                                 \
                float fy, fu, fv;                                                         \
                rgba2yuv_px((float)px[4*c], (float)px[4*c+1], (float)px[4*c+2],           \
                            fscale, fmaxv, &fy, &fu, &fv);                                \
                yr[c] = (OUT_T)fy;                                                        \
                cu[sub][c] = (uint32_t)fu;                                                \
                cv[sub][c] = (uint32_t)fv;                                                \
            }                                                                             \
        }                                                                                 \
        OUT_T *ur = u + (size_t)by * u_stride;                                            \
        OUT_T *vr = v + (size_t)by * v_stride;                                            \
        for (int c = 0; c < w / cw; c++) {                                                \
            uint32_t u0, v0, u1, v1;                                                      \
            if (cw == 2) {                                                                \
                u0 = (cu[0][2*c] + cu[0][2*c+1] + 1) / 2;                                 \
                v0 = (cv[0][2*c] + cv[0][2*c+1] + 1) / 2;                                 \
            } else { u0 = cu[0][c]; v0 = cv[0][c]; }                                      \
            if (ch == 2) {                                                                \
                if (cw == 2) {                                                            \
                    u1 = (cu[1][2*c] + cu[1][2*c+1] + 1) / 2;                             \
                    v1 = (cv[1][2*c] + cv[1][2*c+1] + 1) / 2;                             \
                } else { u1 = cu[1][c]; v1 = cv[1][c]; }                                  \
                u0 = (u0 + u1 + 1) / 2; v0 = (v0 + v1 + 1) / 2;                           \
            }                                                                             \
            ur[c] = (OUT_T)u0; vr[c] = (OUT_T)v0;                                         \
        }                                                                                 \
    }

EXPORT int mvk_rgba2yuv_u8_u8(const uint8_t *restrict rgba, ptrdiff_t rgba_stride,
                              uint8_t *restrict y, ptrdiff_t y_stride,
                              uint8_t *restrict u, ptrdiff_t u_stride,
                              uint8_t *restrict v, ptrdiff_t v_stride,
                              int h, int w, int cw, int ch, int scale, int maxv)
{
    if (w > 8192 || w % cw || h % ch) return -1;
    float fscale = (float)scale, fmaxv = (float)maxv;
    RGBA2YUV_BODY(uint8_t, uint8_t)
    return 0;
}

EXPORT int mvk_rgba2yuv_u8_u16(const uint8_t *restrict rgba, ptrdiff_t rgba_stride,
                               uint16_t *restrict y, ptrdiff_t y_stride,
                               uint16_t *restrict u, ptrdiff_t u_stride,
                               uint16_t *restrict v, ptrdiff_t v_stride,
                               int h, int w, int cw, int ch, int scale, int maxv)
{
    if (w > 8192 || w % cw || h % ch) return -1;
    float fscale = (float)scale, fmaxv = (float)maxv;
    RGBA2YUV_BODY(uint8_t, uint16_t)
    return 0;
}

EXPORT int mvk_rgba2yuv_f32_u8(const float *restrict rgba, ptrdiff_t rgba_stride,
                               uint8_t *restrict y, ptrdiff_t y_stride,
                               uint8_t *restrict u, ptrdiff_t u_stride,
                               uint8_t *restrict v, ptrdiff_t v_stride,
                               int h, int w, int cw, int ch, int scale, int maxv)
{
    if (w > 8192 || w % cw || h % ch) return -1;
    float fscale = (float)scale, fmaxv = (float)maxv;
    RGBA2YUV_BODY(float, uint8_t)
    return 0;
}

EXPORT int mvk_rgba2yuv_f32_u16(const float *restrict rgba, ptrdiff_t rgba_stride,
                                uint16_t *restrict y, ptrdiff_t y_stride,
                                uint16_t *restrict u, ptrdiff_t u_stride,
                                uint16_t *restrict v, ptrdiff_t v_stride,
                                int h, int w, int cw, int ch, int scale, int maxv)
{
    if (w > 8192 || w % cw || h % ch) return -1;
    float fscale = (float)scale, fmaxv = (float)maxv;
    RGBA2YUV_BODY(float, uint16_t)
    return 0;
}

/* --- mixf / mixmap : mix pondéré float32 A/B (ABI 3, plugin mixer) ---------------------- */
/* Équivalents bit-exact des blends de transition du mixer (blend_yuv / blend_yuv_additive /
 * composite keyer / blend_yuv_wipe) : mêmes expressions float32 DANS LE MÊME ORDRE que numpy
 * (fichier compilé -ffp-contract=off), cast final tronqué. Contrairement aux blends entiers
 * du multiview (alpha uint8), ici les coefficients sont FLOTTANTS (position du T-bar / masque
 * de wipe). Tableaux 2D, dernier axe contigu pour dst/a/b (strides lignes en éléments) ; le
 * MASQUE du wipe a en plus un PAS de colonne (m_colstep : la chroma lit a_full[::_CH, ::_CW],
 * vue stridée — on échantillonne le masque plein sans le matérialiser). dst peut ALIASER a
 * (keyer : région relue puis réécrite — élémentwise pur, sûr). do_clip : l'additif clippe la
 * luma [0, maxv] (np.clip AVANT cast) ; le dissolve/chroma ne clippe pas (comme numpy). */

#define MIXF_BODY(T)                                                                       \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int r = 0; r < h; r++) {                                                          \
        T *d = dst + (size_t)r * dst_stride;                                               \
        const T *pa = a + (size_t)r * a_stride;                                            \
        const T *pb = b + (size_t)r * b_stride;                                            \
        for (int c = 0; c < w; c++) {                                                      \
            float v = (float)pa[c] * fa + (float)pb[c] * fb;                               \
            if (do_clip) v = v < 0.0f ? 0.0f : (v > fmaxv ? fmaxv : v);                    \
            d[c] = (T)v;                                                                   \
        }                                                                                  \
    }

EXPORT void mvk_mixf_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                        const uint8_t *a, ptrdiff_t a_stride,
                        const uint8_t *b, ptrdiff_t b_stride,
                        int h, int w, double coef_a, double coef_b, int do_clip, int maxv)
{
    float fa = (float)coef_a, fb = (float)coef_b, fmaxv = (float)maxv;
    MIXF_BODY(uint8_t)
}

EXPORT void mvk_mixf_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                         const uint16_t *a, ptrdiff_t a_stride,
                         const uint16_t *b, ptrdiff_t b_stride,
                         int h, int w, double coef_a, double coef_b, int do_clip, int maxv)
{
    float fa = (float)coef_a, fb = (float)coef_b, fmaxv = (float)maxv;
    MIXF_BODY(uint16_t)
}

/* out = a·(1−m) + b·m, m = masque float32 (wipe). m_colstep : échantillonnage colonne du
 * masque (1 = plein, _CW = chroma sur le masque pleine résolution). Pas de clip (comme numpy). */
#define MIXMAP_BODY(T)                                                                     \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int r = 0; r < h; r++) {                                                          \
        T *d = dst + (size_t)r * dst_stride;                                               \
        const T *pa = a + (size_t)r * a_stride;                                            \
        const T *pb = b + (size_t)r * b_stride;                                            \
        const float *pm = m + (size_t)r * m_stride;                                       \
        for (int c = 0; c < w; c++) {                                                      \
            float fm = pm[(size_t)c * m_colstep];                                          \
            d[c] = (T)((float)pa[c] * (1.0f - fm) + (float)pb[c] * fm);                    \
        }                                                                                  \
    }

EXPORT void mvk_mixmap_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                          const uint8_t *a, ptrdiff_t a_stride,
                          const uint8_t *b, ptrdiff_t b_stride,
                          const float *restrict m, ptrdiff_t m_stride, int64_t m_colstep,
                          int h, int w)
{
    MIXMAP_BODY(uint8_t)
}

EXPORT void mvk_mixmap_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                           const uint16_t *a, ptrdiff_t a_stride,
                           const uint16_t *b, ptrdiff_t b_stride,
                           const float *restrict m, ptrdiff_t m_stride, int64_t m_colstep,
                           int h, int w)
{
    MIXMAP_BODY(uint16_t)
}

/* --- spl_* : compositing du plugin SPLIT / SuperSource (ABI 4) --------------------------- */
/* Le split compose 4 boxes PiP sur un fond ; son mode AVANCÉ (rotation, feather, coins,
 * opacité, miroir, bordure 3D) était le DERNIER compositeur resté en numpy pur. Poste n°1
 * mesuré (banc nœud 30, 1080p, 4 boxes) : la ROTATION — 31,9 ms/trame, HORS du budget 50p
 * (20 ms). Décomposition par box : gather 2D (np.take sur la carte d'index de l'AABB) 3,6 ms
 * + blends 3,5 ms = 7,1 ms. Ces kernels fusionnent gather + ruban de bordure + blend en UNE
 * passe mémoire (l'op est memory-bound : chaque passe numpy = un aller-retour RAM complet).
 *
 * ⚠ ARITHMÉTIQUE ≠ celle des blends multiview : le split travaille en 256e (alpha 0..256,
 * décalage >> 8), pas en 255e (division par 255). Ne PAS réutiliser mvk_blend_* ici : les
 * octets diffèreraient. Accumulation en uint32 (numpy accumulait en uint16 : correct en 8
 * bits — max 255·256 = 65280 — mais DÉBORDANT en 10/12 bits ; le repli numpy du plugin a été
 * porté en uint32 avec ces kernels, cf. plugins/split/script.py).
 *
 * Deux modes d'index, choisis par l'appelant (bit-exact à SA formule nearest, comme mvk_place) :
 *   - flat != NULL : carte d'index PLAT 2D int64 (box TOURNÉE, échantillonnage diagonal) ;
 *   - flat == NULL : row_idx[r] / col_idx[c] int32 séparables (box droite).
 * Ruban de bordure (ring_a/ring_c) et masque alpha (alpha, sinon a_scalar 0..256) optionnels.
 * Strides en ÉLÉMENTS ; dernier axe contigu (sinon le wrapper bobimxl renvoie False → numpy).
 */

#define SPL_COMPOSE_BODY(T)                                                                 \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int r = 0; r < h; r++) {                                                           \
        T *d = dst + (size_t)r * dst_stride;                                                \
        const T *srow = flat ? NULL : src + (size_t)row_idx[r] * src_stride;                \
        const uint16_t *ra = ring_a ? ring_a + (size_t)r * ra_stride : NULL;                \
        const T *rc = ring_c ? ring_c + (size_t)r * rc_stride : NULL;                       \
        const uint16_t *av = alpha ? alpha + (size_t)r * a_stride : NULL;                   \
        const int64_t *fl = flat ? flat + (size_t)r * flat_stride : NULL;                   \
        for (int c = 0; c < w; c++) {                                                       \
            uint32_t k = fl ? (uint32_t)src[fl[c]] : (uint32_t)srow[col_idx[c]];            \
            if (ra) {                                                                       \
                uint32_t g = ra[c];                                                         \
                if (g) k = (k * (256u - g) + (uint32_t)rc[c] * g) >> 8;                     \
            }                                                                               \
            uint32_t a = av ? av[c] : (uint32_t)a_scalar;                                   \
            d[c] = (T)(a >= 256u ? k : (((uint32_t)d[c] * (256u - a) + k * a) >> 8));       \
        }                                                                                   \
    }

EXPORT void mvk_spl_compose_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                               const uint8_t *restrict src, ptrdiff_t src_stride,
                               const int64_t *restrict flat, ptrdiff_t flat_stride,
                               const int32_t *restrict row_idx, const int32_t *restrict col_idx,
                               const uint16_t *restrict ring_a, ptrdiff_t ra_stride,
                               const uint8_t *restrict ring_c, ptrdiff_t rc_stride,
                               const uint16_t *restrict alpha, ptrdiff_t a_stride,
                               int a_scalar, int h, int w)
{
    SPL_COMPOSE_BODY(uint8_t)
}

EXPORT void mvk_spl_compose_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                                const uint16_t *restrict src, ptrdiff_t src_stride,
                                const int64_t *restrict flat, ptrdiff_t flat_stride,
                                const int32_t *restrict row_idx, const int32_t *restrict col_idx,
                                const uint16_t *restrict ring_a, ptrdiff_t ra_stride,
                                const uint16_t *restrict ring_c, ptrdiff_t rc_stride,
                                const uint16_t *restrict alpha, ptrdiff_t a_stride,
                                int a_scalar, int h, int w)
{
    SPL_COMPOSE_BODY(uint16_t)
}

/* fused : opérandes PRÉ-FUSIONNÉS du stamp (bordure + alpha en une passe, cf. _build_stamp) —
 * dst = clip( (dst·inv_a >> 8) + (src[idx]·A1 >> 8) + C2 , 0, maxv ). Arithmétique uint32
 * IDENTIQUE au numpy du plugin (qui promeut déjà en uint32 sur ce chemin). */

#define SPL_FUSED_BODY(T)                                                                   \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int r = 0; r < h; r++) {                                                           \
        T *d = dst + (size_t)r * dst_stride;                                                \
        const T *srow = flat ? NULL : src + (size_t)row_idx[r] * src_stride;                \
        const int64_t *fl = flat ? flat + (size_t)r * flat_stride : NULL;                   \
        const uint16_t *ia = inv_a + (size_t)r * ia_stride;                                 \
        const uint16_t *a1 = a1_map + (size_t)r * a1_stride;                                \
        const uint32_t *c2 = c2_map + (size_t)r * c2_stride;                                \
        for (int c = 0; c < w; c++) {                                                       \
            uint32_t k = fl ? (uint32_t)src[fl[c]] : (uint32_t)srow[col_idx[c]];            \
            uint32_t v = (((uint32_t)d[c] * ia[c]) >> 8) + ((k * a1[c]) >> 8) + c2[c];      \
            d[c] = (T)(v > (uint32_t)maxv ? (uint32_t)maxv : v);                            \
        }                                                                                   \
    }

EXPORT void mvk_spl_fused_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                             const uint8_t *restrict src, ptrdiff_t src_stride,
                             const int64_t *restrict flat, ptrdiff_t flat_stride,
                             const int32_t *restrict row_idx, const int32_t *restrict col_idx,
                             const uint16_t *restrict inv_a, ptrdiff_t ia_stride,
                             const uint16_t *restrict a1_map, ptrdiff_t a1_stride,
                             const uint32_t *restrict c2_map, ptrdiff_t c2_stride,
                             int h, int w, int maxv)
{
    SPL_FUSED_BODY(uint8_t)
}

EXPORT void mvk_spl_fused_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                              const uint16_t *restrict src, ptrdiff_t src_stride,
                              const int64_t *restrict flat, ptrdiff_t flat_stride,
                              const int32_t *restrict row_idx, const int32_t *restrict col_idx,
                              const uint16_t *restrict inv_a, ptrdiff_t ia_stride,
                              const uint16_t *restrict a1_map, ptrdiff_t a1_stride,
                              const uint32_t *restrict c2_map, ptrdiff_t c2_stride,
                              int h, int w, int maxv)
{
    SPL_FUSED_BODY(uint16_t)
}

/* solid : blend d'une COULEUR UNIE sous masque (ombre portée du split, couche ART opaque/frange).
 * dst = (dst·(256−a) + val·a) >> 8. Une passe, uint32 (cf. note de débordement plus haut). */

#define SPL_SOLID_BODY(T)                                                                   \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)") \
    for (int r = 0; r < h; r++) {                                                           \
        T *d = dst + (size_t)r * dst_stride;                                                \
        const uint16_t *av = alpha + (size_t)r * a_stride;                                  \
        for (int c = 0; c < w; c++) {                                                       \
            uint32_t a = av[c];                                                             \
            if (a) d[c] = (T)(((uint32_t)d[c] * (256u - a) + (uint32_t)val * a) >> 8);      \
        }                                                                                   \
    }

EXPORT void mvk_spl_solid_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
                             const uint16_t *restrict alpha, ptrdiff_t a_stride,
                             int val, int h, int w)
{
    SPL_SOLID_BODY(uint8_t)
}

EXPORT void mvk_spl_solid_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
                              const uint16_t *restrict alpha, ptrdiff_t a_stride,
                              int val, int h, int w)
{
    SPL_SOLID_BODY(uint16_t)
}

/* --- spl_gradient : fond dégradé N arrêts du plugin SPLIT (ABI 5) ----------------------------
 * Un fond DÉGRADÉ est une valeur PAR PIXEL → passe plein cadre. Statique il est calculé une fois
 * et caché ; ANIMÉ (wipe qui balaie pendant une transition) il est refait à CHAQUE trame — d'où
 * le C (le même travail en numpy est ruineux, cf. l'ART en source vive : 21,7 ms/trame en 1080p).
 *
 * MODÈLE UNIFIÉ mélange/wipe : `softness` module la RAIDEUR de la rampe entre deux arrêts.
 *   softness = 0   → marche d'escalier au milieu du segment = WIPE (frontière nette) ;
 *   softness = 1   → rampe linéaire = dégradé progressif ;
 *   entre          → rampe linéaire de largeur `softness` centrée au milieu (transition molle).
 * ⇒ un wipe n'est qu'un dégradé de douceur nulle : UNE fonctionnalité, pas deux.
 *
 * `t(x,y)` = projection scalaire du CENTRE du pixel sur l'axe (angle), normalisée [0,1] sur le
 * cadre (pmin/inv_range précalculés côté Python sur les 4 coins). Les arrêts (positions triées +
 * couleurs Y/U/V DÉJÀ converties à la profondeur/colorimétrie par l'appelant) sont interpolés en
 * float32. TRAMAGE (dither ordonné Bayer 8×8, amplitude ±½ LSB) ajouté avant l'arrondi : sans lui
 * un dégradé plat MONTRE DES BANDES en 8 bits (défaut classique, visible à l'antenne). La table
 * Bayer (64 flottants, déjà centrée) est passée par l'appelant → identique au repli numpy.
 *
 * ★ BIT-EXACT AVEC LE REPLI NUMPY (impératif — un repli qui diverge du kernel est un bug invisible
 * au banc) : tout est en float32, MÊME ordre d'opérations, arrondi = floorf(v + 0,5f), fichier
 * compilé -ffp-contract=off. Le repli `_bg_gradient_np` reproduit ces expressions à l'identique.
 *
 * MODE TRANCHE : le kernel ne remplit que la BANDE [y0,y1) (luma) et [y0/ch, y1/ch) (chroma) — le
 * `t` d'une bande est un sous-ensemble contigu, aucun état entre bandes. y0/y1 multiples de ch. */

/* ★ Calcul en DOUBLE (et non float) : le repli numpy est en float64 par défaut, et la
 * promotion float32→double est exacte → mêmes octets à condition d'un ORDRE d'opérations
 * identique et de -ffp-contract=off (pas de FMA). C'est ce qui rend kernel ≡ repli PROUVABLE
 * (impératif : un repli qui diverge est un bug invisible au banc). Le repli `_bg_gradient_np`
 * reproduit ces expressions à l'identique, terme à terme. */
static inline void grad_at(double t, int nst, const float *restrict pos,
                           const float *restrict yv, const float *restrict uv,
                           const float *restrict vv, double softness,
                           double *oy, double *ou, double *ov)
{
    if (t <= (double)pos[0])        { *oy = yv[0]; *ou = uv[0]; *ov = vv[0]; return; }
    if (t >= (double)pos[nst - 1])  { *oy = yv[nst-1]; *ou = uv[nst-1]; *ov = vv[nst-1]; return; }
    int i = 0;
    while (i < nst - 2 && t >= (double)pos[i + 1]) i++;
    double seg = (double)pos[i + 1] - (double)pos[i];
    double local = seg > 1e-9 ? (t - (double)pos[i]) / seg : 0.0;
    double w;
    if (softness <= 1e-6) {
        w = local < 0.5 ? 0.0 : 1.0;                    /* WIPE : marche au milieu du segment */
    } else {
        w = (local - 0.5) / softness + 0.5;
        w = w < 0.0 ? 0.0 : (w > 1.0 ? 1.0 : w);
    }
    double iw = 1.0 - w;
    *oy = (double)yv[i] * iw + (double)yv[i + 1] * w;
    *ou = (double)uv[i] * iw + (double)uv[i + 1] * w;
    *ov = (double)vv[i] * iw + (double)vv[i + 1] * w;
}

#define SPL_GRAD_BODY(T)                                                                   \
    const int cw0 = W / cw, ch0 = ch;                                                       \
    const double dcos = (double)cosv, dsin = (double)sinv, dpmin = (double)pmin,            \
                 dinv = (double)inv_range, dsoft = (double)softness, damp = (double)amp;    \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)(y1 - y0) * W >= PAR_MIN)") \
    for (int y = y0; y < y1; y++) {                                                         \
        T *yr = yp + (size_t)y * ystride;                                                   \
        const float *br = bayer + (size_t)(y & 7) * 8;                                      \
        double py = ((double)y + 0.5) * dsin;                                               \
        for (int x = 0; x < W; x++) {                                                       \
            double t = (((double)x + 0.5) * dcos + py - dpmin) * dinv;                      \
            t = t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t);                                        \
            double gy, gu, gv;                                                              \
            grad_at(t, nst, pos, yv, uv, vv, dsoft, &gy, &gu, &gv);                         \
            double d = damp * (double)br[x & 7];                                            \
            int iy = (int)floor(gy + d + 0.5);                                              \
            yr[x] = (T)(iy < 0 ? 0 : (iy > maxv ? maxv : iy));                              \
        }                                                                                   \
    }                                                                                       \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads) if(g_threads > 1 && (int64_t)((y1 - y0) / ch0) * cw0 >= PAR_MIN)") \
    for (int cy = y0 / ch0; cy < y1 / ch0; cy++) {                                          \
        T *ur = up + (size_t)cy * ustride;                                                  \
        T *vr = vp + (size_t)cy * vstride;                                                  \
        const float *br = bayer + (size_t)(cy & 7) * 8;                                     \
        double ly = (double)cy * ch0 + ch0 * 0.5;                                           \
        double py = ly * dsin;                                                              \
        for (int cx = 0; cx < cw0; cx++) {                                                  \
            double lx = (double)cx * cw + cw * 0.5;                                         \
            double t = (lx * dcos + py - dpmin) * dinv;                                     \
            t = t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t);                                        \
            double gy, gu, gv;                                                              \
            grad_at(t, nst, pos, yv, uv, vv, dsoft, &gy, &gu, &gv);                         \
            double d = damp * (double)br[cx & 7];                                           \
            int iu = (int)floor(gu + d + 0.5);                                              \
            int iv = (int)floor(gv + d + 0.5);                                              \
            ur[cx] = (T)(iu < 0 ? 0 : (iu > maxv ? maxv : iu));                             \
            vr[cx] = (T)(iv < 0 ? 0 : (iv > maxv ? maxv : iv));                             \
        }                                                                                   \
    }

EXPORT void mvk_spl_gradient_u8(uint8_t *restrict yp, ptrdiff_t ystride,
                                uint8_t *restrict up, ptrdiff_t ustride,
                                uint8_t *restrict vp, ptrdiff_t vstride,
                                int W, int H, int cw, int ch, int y0, int y1,
                                float cosv, float sinv, float pmin, float inv_range,
                                int nst, const float *restrict pos, const float *restrict yv,
                                const float *restrict uv, const float *restrict vv,
                                float softness, int maxv, float amp, const float *restrict bayer)
{
    (void)H;
    SPL_GRAD_BODY(uint8_t)
}

EXPORT void mvk_spl_gradient_u16(uint16_t *restrict yp, ptrdiff_t ystride,
                                 uint16_t *restrict up, ptrdiff_t ustride,
                                 uint16_t *restrict vp, ptrdiff_t vstride,
                                 int W, int H, int cw, int ch, int y0, int y1,
                                 float cosv, float sinv, float pmin, float inv_range,
                                 int nst, const float *restrict pos, const float *restrict yv,
                                 const float *restrict uv, const float *restrict vv,
                                 float softness, int maxv, float amp, const float *restrict bayer)
{
    (void)H;
    SPL_GRAD_BODY(uint16_t)
}

/* ═══════════════════════════════════════════════════════════════════════════════════════════
 *  CONSTRUCTION D'EMPREINTE (split) — ABI 6
 *
 *  Ces kernels ne composent RIEN : ils bâtissent l'EMPREINTE (stamp) d'une box. Ils n'existent
 *  que pour le régime ANIMÉ. En régime établi le cache de stamp rend cette construction
 *  gratuite ; mais dès que la géométrie bouge à chaque trame (transition, curseur qu'on glisse,
 *  rotation animée) elle est refaite 50 fois par seconde et DOMINE la trame — mesuré sur dell-1,
 *  1080p, 4 boxes, tous effets : 208 ms sur 230 ms de trame, soit 90 %.
 *
 *  ★ BIT-EXACTITUDE : chaque kernel reproduit l'ORDRE des opérations flottantes du repli numpy
 *  — accumulation float32 SÉQUENTIELLE pour les sommes préfixes, scalaires rabattus en float
 *  AVANT l'opération (promotion faible de numpy : un scalaire Python n'élargit pas un tableau
 *  float32), division (jamais multiplication par l'inverse), troncature vers zéro pour les casts
 *  entiers. Compilé avec -ffp-contract=off — une FMA changerait le dernier bit.
 * ═══════════════════════════════════════════════════════════════════════════════════════════ */

static inline float mvk_clampf(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* Flou ~gaussien 2D d'un masque NON séparable (silhouette d'une box tournée) : `iters` box-blurs,
 * chacun séparé en 2 passes 1D (lignes puis colonnes). Réplique exactement, par passe :
 *     c = np.cumsum(np.pad(m, r), dtype=np.float32) ; m = (c[2r:] - c[:-2r]) / (2r)
 * Avec p[0..r-1] = 0, p[r+i] = m[i] : c[k<r] = 0 et c[k>=r] = S[min(k-r, h-1)], où S est la somme
 * préfixe float32 de m. D'où out[y] = (S[min(y+r,h-1)] - (y>=r ? S[y-r] : 0)) / (2r).
 * `scratch` : h*w float fournis par l'appelant — le kernel n'alloue rien. */
EXPORT void mvk_spl_blur2_f32(float *restrict m, int h, int w, int r, int iters,
                              float *restrict scratch)
{
    if (r < 1) r = 1;
    if (h < 1 || w < 1) return;
    const float den = (float)(2 * r);
    for (int it = 0; it < iters; it++) {
        /* ── passe VERTICALE (axis=0) : préfixe le long des colonnes.
         * ★ Parcours par BLOCS DE COLONNES, lignes à l'intérieur : un préfixe colonne par
         * colonne (boucle y interne, pas de w) rate le cache à chaque itération — mesuré
         * 20,4 ms/trame sur cette seule passe. L'accumulation reste SÉQUENTIELLE par colonne
         * (scratch[y] = scratch[y-1] + m[y]) donc les octets sont inchangés. */
        #define MVK_BLUR_CB 512
        {
            const int nb = (w + MVK_BLUR_CB - 1) / MVK_BLUR_CB;
            #pragma omp parallel for schedule(static) num_threads(g_threads)
            for (int b = 0; b < nb; b++) {
                const int c0 = b * MVK_BLUR_CB;
                const int c1 = (c0 + MVK_BLUR_CB < w) ? (c0 + MVK_BLUR_CB) : w;
                for (int c = c0; c < c1; c++) scratch[c] = m[c];
                for (int y = 1; y < h; y++) {
                    const float *mm = m + (size_t)y * w;
                    const float *pv = scratch + (size_t)(y - 1) * w;
                    float *sc = scratch + (size_t)y * w;
                    for (int c = c0; c < c1; c++) sc[c] = pv[c] + mm[c];
                }
            }
        }
        #pragma omp parallel for schedule(static) num_threads(g_threads)
        for (int y = 0; y < h; y++) {
            const int hi = (y + r < h - 1) ? (y + r) : (h - 1);
            const float *shi = scratch + (size_t)hi * w;
            float *out = m + (size_t)y * w;
            if (y >= r) {
                const float *slo = scratch + (size_t)(y - r) * w;
                for (int c = 0; c < w; c++) out[c] = (shi[c] - slo[c]) / den;
            } else {
                for (int c = 0; c < w; c++) out[c] = shi[c] / den;
            }
        }
        /* ── passe HORIZONTALE (axis=1) ── */
        #pragma omp parallel for schedule(static) num_threads(g_threads)
        for (int y = 0; y < h; y++) {
            float *row = m + (size_t)y * w;
            float *sc = scratch + (size_t)y * w;
            float acc = 0.0f;
            for (int c = 0; c < w; c++) { acc += row[c]; sc[c] = acc; }
            for (int c = 0; c < w; c++) {
                const int hi = (c + r < w - 1) ? (c + r) : (w - 1);
                row[c] = (c >= r ? (sc[hi] - sc[c - r]) : sc[hi]) / den;
            }
        }
    }
}

/* Empreinte d'une box TOURNÉE : carte d'échantillonnage (index PLAT luma + chroma), masque alpha
 * (feather × coins arrondis × opacité), distance signée aux bords (réutilisée par l'ombre et le
 * ruban), et max d'index source par ligne (mode tranche). UNE passe sur l'AABB au lieu d'une
 * quinzaine de tableaux temporaires numpy.
 *   ys/yc (ah) et xc/nxs (aw) = rampes précalculées : ul = ys[j] + xc[i], vl = yc[j] + nxs[i]. */
EXPORT void mvk_spl_rotmap(int aw, int ah,
                           const float *restrict ys, const float *restrict yc,
                           const float *restrict xc, const float *restrict nxs,
                           float hw, float hh, float fdw, float fdh, int mh, int mv,
                           float sxr0, float dsx, float syr0, float dsy,
                           int W, int H, int UVW, int UVH, int cw, int ch,
                           int use_fe, float fe, int rad, float radf,
                           float cref_x, float cref_y, float csoft, float cadd,
                           int apply_op, float op,
                           int64_t *restrict flat, int64_t *restrict flat2,
                           uint16_t *restrict a16, uint16_t *restrict a16_2,
                           float *restrict dedge_out, int32_t *restrict rowmax)
{
    const int aw2 = (aw + cw - 1) / cw;
    const float fW = (float)(W - 1), fH = (float)(H - 1);
    #pragma omp parallel for schedule(static) num_threads(g_threads)
    for (int j = 0; j < ah; j++) {
        const float ysj = ys[j], ycj = yc[j];
        const size_t base = (size_t)j * aw;
        int32_t rmax = 0;
        for (int i = 0; i < aw; i++) {
            const float ul = ysj + xc[i];
            const float vl = ycj + nxs[i];
            const float aul = fabsf(ul), avl = fabsf(vl);
            const float dxl = hw - aul, dyl = hh - avl;
            const float de = dxl < dyl ? dxl : dyl;

            float fu = ul / fdw + 0.5f;  if (mh) fu = 1.0f - fu;
            float fv = vl / fdh + 0.5f;  if (mv) fv = 1.0f - fv;
            const float mx = mvk_clampf(sxr0 + fu * dsx, 0.0f, fW);
            const float my = mvk_clampf(syr0 + fv * dsy, 0.0f, fH);
            const int32_t mxi = (int32_t)mx, myi = (int32_t)my;
            flat[base + i] = (int64_t)myi * W + mxi;
            if (myi > rmax) rmax = myi;

            float a = use_fe ? mvk_clampf(de / fe, 0.0f, 1.0f)
                             : mvk_clampf(de + 0.5f, 0.0f, 1.0f);
            if (rad > 0) {
                float cx = aul - cref_x; if (cx < 0.0f) cx = 0.0f;
                float cy = avl - cref_y; if (cy < 0.0f) cy = 0.0f;
                const float rr = sqrtf(cx * cx + cy * cy);
                a *= mvk_clampf((radf - rr) / csoft + cadd, 0.0f, 1.0f);
            }
            if (apply_op) a *= op;
            a16[base + i] = (uint16_t)(a * 256.0f);

            if (dedge_out) dedge_out[base + i] = de;
            if ((j % ch) == 0 && (i % cw) == 0) {
                const size_t k2 = (size_t)(j / ch) * aw2 + (i / cw);
                a16_2[k2] = a16[base + i];
                int32_t y2 = myi / ch; if (y2 > UVH - 1) y2 = UVH - 1;
                int32_t x2 = mxi / cw; if (x2 > UVW - 1) x2 = UVW - 1;
                flat2[k2] = (int64_t)y2 * UVW + x2;
            }
        }
        if (rowmax) rowmax[j] = rmax;
    }
}

/* Ruban de bordure 3D d'une box TOURNÉE : le ruban suit les bords OBLIQUES et la normale du
 * biseau tourne avec la box (sinon le relief resterait éclairé comme si la box était droite).
 * Remplace ~10 tableaux 2D numpy (where/sign/clip/abs) par une passe. */
EXPORT void mvk_spl_rotring_u8(int aw, int ah,
                               const float *restrict ys, const float *restrict yc,
                               const float *restrict xc, const float *restrict nxs,
                               float hw, float hh, float sv, float cv,
                               float inner_w, int has_bevel, float bevel_abs, float sgn,
                               float pos_iw, float soft_div, float lx, float ly, float lz,
                               float Yb, float minv, float maxv,
                               uint16_t *restrict ring_a, uint8_t *restrict ring_y)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads)
    for (int j = 0; j < ah; j++) {
        const float ysj = ys[j], ycj = yc[j];
        const size_t base = (size_t)j * aw;
        for (int i = 0; i < aw; i++) {
            const float ul = ysj + xc[i], vl = ycj + nxs[i];
            const float dxl = hw - fabsf(ul), dyl = hh - fabsf(vl);
            const float de = dxl < dyl ? dxl : dyl;
            float ring = mvk_clampf(inner_w - de, 0.0f, 1.0f);
            if (!(de > 0.0f)) ring = 0.0f;
            ring_a[base + i] = (uint16_t)(ring * 256.0f);
            if (has_bevel) {
                const int near_x = (dyl < dxl);
                const float nlx = near_x ? 0.0f : (ul > 0.0f ? 1.0f : (ul < 0.0f ? -1.0f : 0.0f));
                const float nly = near_x ? (vl > 0.0f ? 1.0f : (vl < 0.0f ? -1.0f : 0.0f)) : 0.0f;
                const float gx = nlx * cv - nly * sv;
                const float gy = nlx * sv + nly * cv;
                const float shade = mvk_clampf((lx * gx + ly * gy) * sgn * bevel_abs + lz, 0.0f, 1.0f);
                const float t = mvk_clampf((de - pos_iw) / soft_div + 0.5f, 0.0f, 1.0f);
                const float prof = 0.35f + 0.65f * (1.0f - fabsf(2.0f * t - 1.0f));
                const float lit = mvk_clampf(shade * prof * 2.0f, 0.0f, 1.6f);
                ring_y[base + i] = (uint8_t)mvk_clampf(Yb * lit, minv, maxv);
            } else {
                ring_y[base + i] = (uint8_t)Yb;
            }
        }
    }
}

EXPORT void mvk_spl_rotring_u16(int aw, int ah,
                                const float *restrict ys, const float *restrict yc,
                                const float *restrict xc, const float *restrict nxs,
                                float hw, float hh, float sv, float cv,
                                float inner_w, int has_bevel, float bevel_abs, float sgn,
                                float pos_iw, float soft_div, float lx, float ly, float lz,
                                float Yb, float minv, float maxv,
                                uint16_t *restrict ring_a, uint16_t *restrict ring_y)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads)
    for (int j = 0; j < ah; j++) {
        const float ysj = ys[j], ycj = yc[j];
        const size_t base = (size_t)j * aw;
        for (int i = 0; i < aw; i++) {
            const float ul = ysj + xc[i], vl = ycj + nxs[i];
            const float dxl = hw - fabsf(ul), dyl = hh - fabsf(vl);
            const float de = dxl < dyl ? dxl : dyl;
            float ring = mvk_clampf(inner_w - de, 0.0f, 1.0f);
            if (!(de > 0.0f)) ring = 0.0f;
            ring_a[base + i] = (uint16_t)(ring * 256.0f);
            if (has_bevel) {
                const int near_x = (dyl < dxl);
                const float nlx = near_x ? 0.0f : (ul > 0.0f ? 1.0f : (ul < 0.0f ? -1.0f : 0.0f));
                const float nly = near_x ? (vl > 0.0f ? 1.0f : (vl < 0.0f ? -1.0f : 0.0f)) : 0.0f;
                const float gx = nlx * cv - nly * sv;
                const float gy = nlx * sv + nly * cv;
                const float shade = mvk_clampf((lx * gx + ly * gy) * sgn * bevel_abs + lz, 0.0f, 1.0f);
                const float t = mvk_clampf((de - pos_iw) / soft_div + 0.5f, 0.0f, 1.0f);
                const float prof = 0.35f + 0.65f * (1.0f - fabsf(2.0f * t - 1.0f));
                const float lit = mvk_clampf(shade * prof * 2.0f, 0.0f, 1.6f);
                ring_y[base + i] = (uint16_t)mvk_clampf(Yb * lit, minv, maxv);
            } else {
                ring_y[base + i] = (uint16_t)Yb;
            }
        }
    }
}

/* ── PRÉ-FUSION ruban ⊗ alpha (split, chemin NON TOURNÉ) ──────────────────────────────────────
 * Quand une box a une opacité globale, il n'y a pas d'intérieur opaque exploitable : le
 * compositing ferait DEUX balayages pleins par plan (peindre la bordure, puis fondre). Le plugin
 * les pré-fusionne donc hors trame :
 *     sortie = out·(1−a) + src·[(1−ring)·a] + [couleur·ring·a]
 *              └ inv_a ┘       └── A1 ──┘     └────── C2 ──────┘
 * ★ L'IRONIE MESURÉE : cette pré-fusion existe pour rendre la trame RAPIDE, et c'est elle qui
 * rend la RECONSTRUCTION lente — nef arrays uint16/uint32 plein rect, ~20 passes mémoire, 17 ms
 * sur les 41 ms d'une trame animée en 1080p (4 boxes). Invisible tant que la géométrie ne bouge
 * pas (tout est en cache), dominante dès qu'elle bouge.
 * Arithmétique ENTIÈRE identique au repli numpy (accumulation uint32, décalage de 8) → bit-exact
 * par construction. `inv_a`/`A1` peuvent être NULL (déjà calculés par un appel précédent : les
 * plans U et V partagent leur alpha et ne diffèrent que par la couleur du ruban).
 */
#define SPL_PREFUSE_BODY(T)                                                                    \
    _Pragma("omp parallel for schedule(static) num_threads(g_threads)")                        \
    for (int r = 0; r < h; r++) {                                                              \
        const uint16_t *av = a + (size_t)r * a_stride;                                         \
        const uint16_t *rg = ring_a + (size_t)r * ra_stride;                                   \
        const T *rc = ring_c + (size_t)r * rc_stride;                                          \
        uint16_t *ia = inv_a ? inv_a + (size_t)r * ia_stride : NULL;                           \
        uint16_t *a1 = A1 ? A1 + (size_t)r * a1_stride : NULL;                                 \
        uint32_t *c2 = C2 + (size_t)r * c2_stride;                                             \
        for (int c = 0; c < w; c++) {                                                          \
            const uint32_t A = av[c], G = rg[c];                                                \
            if (ia) ia[c] = (uint16_t)(256u - A);                                              \
            if (a1) a1[c] = (uint16_t)((A * (256u - G)) >> 8);                                 \
            c2[c] = (((uint32_t)rc[c] * ((A * G) >> 8)) >> 8);                                 \
        }                                                                                      \
    }

EXPORT void mvk_spl_prefuse_u8(const uint16_t *restrict a, ptrdiff_t a_stride,
                               const uint16_t *restrict ring_a, ptrdiff_t ra_stride,
                               const uint8_t *restrict ring_c, ptrdiff_t rc_stride,
                               uint16_t *restrict inv_a, ptrdiff_t ia_stride,
                               uint16_t *restrict A1, ptrdiff_t a1_stride,
                               uint32_t *restrict C2, ptrdiff_t c2_stride, int h, int w)
{
    SPL_PREFUSE_BODY(uint8_t)
}

EXPORT void mvk_spl_prefuse_u16(const uint16_t *restrict a, ptrdiff_t a_stride,
                                const uint16_t *restrict ring_a, ptrdiff_t ra_stride,
                                const uint16_t *restrict ring_c, ptrdiff_t rc_stride,
                                uint16_t *restrict inv_a, ptrdiff_t ia_stride,
                                uint16_t *restrict A1, ptrdiff_t a1_stride,
                                uint32_t *restrict C2, ptrdiff_t c2_stride, int h, int w)
{
    SPL_PREFUSE_BODY(uint16_t)
}

/* Produit externe → uint16, en une passe : out[j][i] = (uint16)(vy[j] · vx[i]).
 * Le masque d'ombre d'une box DROITE est séparable (créneau flouté × créneau flouté) — mais sa
 * MATÉRIALISATION reste un tableau plein rect élargi (~500 k px), que numpy produit en deux
 * passes (multiply.outer puis astype). Ici : une. */
EXPORT void mvk_spl_outer_u16(uint16_t *restrict out, ptrdiff_t o_stride,
                              const float *restrict vy, const float *restrict vx, int h, int w)
{
    #pragma omp parallel for schedule(static) num_threads(g_threads) \
            if(g_threads > 1 && (int64_t)h * w >= PAR_MIN)
    for (int r = 0; r < h; r++) {
        const float y = vy[r];
        uint16_t *o = out + (size_t)r * o_stride;
        for (int c = 0; c < w; c++) o[c] = (uint16_t)(y * vx[c]);
    }
}
