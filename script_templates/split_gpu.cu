/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * split_gpu.cu — compositing DVE du plugin `split` sur GPU (chargé par cupy/NVRTC).
 *
 * ★ CE QUI CHANGE PAR RAPPORT AU CHEMIN CPU, et c'est tout le sujet : il n'y a PAS D'EMPREINTE.
 *   Pas de carte d'index, pas de masque de feather, pas de ruban précalculé, pas de masque
 *   d'ombre, pas de cache de stamp, pas de clé de cache. Chaque pixel de sortie évalue sa propre
 *   géométrie par rotation inverse, son propre alpha et son propre ruban. Conséquence directe :
 *   le coût est le MÊME que la géométrie soit fixe ou qu'elle change à chaque trame. La
 *   distinction « régime établi / régime animé », qui structure tout le chemin CPU et qui lui
 *   coûte 227 ms/trame en 1080p quand la géométrie bouge, n'existe pas ici.
 *
 * ⚠ CE N'EST PAS UN PORTAGE BIT-EXACT du chemin CPU, et ça ne peut pas l'être : le CPU mélange en
 *   entiers 256e, le GPU en flottant. C'est un CHEMIN DE RENDU DISTINCT, choisi explicitement par
 *   `render=gpu` — les octets d'une composition existante changent si on bascule.
 *
 * Ce fichier vit dans script_templates/ (comme mvcompose.c) et non dans le script du plugin :
 * script.py est un template str.format, où chaque accolade littérale devrait être doublée.
 *
 * Conventions : plans planar 8 ou 16 bits (T = unsigned char / unsigned short), un kernel par
 * plan (le sous-échantillonnage chroma passe par cw/ch), coordonnées locales en pixels LUMA.
 */

#define SPL_CLAMPF(v, lo, hi) ((v) < (lo) ? (lo) : ((v) > (hi) ? (hi) : (v)))

/* ── Composition d'UNE box sur UN plan ────────────────────────────────────────────────────────
 * Un thread = un pixel de l'AABB de la box. Hors du quadrilatère incliné, alpha vaut 0 et le
 * thread sort sans écrire : le fond reste visible.
 */
template <typename T>
__device__ __forceinline__ void spl_box_plane(
    T* __restrict__ dst, int dstW, int dstH,
    const T* __restrict__ src, int srcW, int srcH,
    int x0, int y0, int aw, int ah,          /* AABB, en pixels DU PLAN */
    int cw, int ch,                          /* diviseurs chroma (1,1 pour le luma) */
    float sv, float cvv,                     /* sin/cos de la rotation */
    float hw, float hh,                      /* demi-dimensions de la box, en px LUMA */
    float fdw, float fdh,                    /* dimensions de la box, en px LUMA */
    int mh, int mv,                          /* miroirs */
    float sxr0, float dsx, float syr0, float dsy,   /* région source (crop), en px DU PLAN */
    int use_fe, float fe, float radf, float cref_x, float cref_y,
    float csoft, float cadd, float op,
    float inner_w, int has_bevel, float bevel_abs, float sgn,
    float pos_iw, float soft_div, float lx, float ly, float lz,
    float ringc, float minv, float maxv)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    if (i >= aw || j >= ah) return;
    const int X = x0 + i, Y = y0 + j;
    if (X < 0 || X >= dstW || Y < 0 || Y >= dstH) return;

    /* Rotation INVERSE : (écran) → (local box), en pixels LUMA. C'est tout ce que la carte
       d'index du chemin CPU précalculait et stockait : ici, quatre multiplications. */
    const float xf = (float)(i * cw) - (float)(aw * cw - 1) * 0.5f;
    const float yf = (float)(j * ch) - (float)(ah * ch - 1) * 0.5f;
    const float ul = yf * sv + xf * cvv;
    const float vl = yf * cvv - xf * sv;
    const float aul = fabsf(ul), avl = fabsf(vl);
    const float dxl = hw - aul, dyl = hh - avl;
    const float de = fminf(dxl, dyl);

    /* ALPHA = feather × coins arrondis × opacité × fondu de transition. Les bords obliques sont
       antialiasés par construction (clip(distance + ½) donne une transition d'un pixel). */
    float a = use_fe ? SPL_CLAMPF(de / fe, 0.0f, 1.0f) : SPL_CLAMPF(de + 0.5f, 0.0f, 1.0f);
    if (radf > 0.0f) {
        const float cx = fmaxf(0.0f, aul - cref_x);
        const float cy = fmaxf(0.0f, avl - cref_y);
        a *= SPL_CLAMPF((radf - sqrtf(cx * cx + cy * cy)) / csoft + cadd, 0.0f, 1.0f);
    }
    a *= op;
    if (a <= 0.0f) return;

    /* ÉCHANTILLONNAGE plus-proche-voisin — le même que le CPU, pour que basculer de chemin ne
       change pas le rendu perçu au-delà de l'arrondi. (Le bilinéaire serait ici quasi gratuit :
       c'est le gain de QUALITÉ que le GPU rend accessible, à activer explicitement.) */
    float fu = ul / fdw + 0.5f;  if (mh) fu = 1.0f - fu;
    float fv = vl / fdh + 0.5f;  if (mv) fv = 1.0f - fv;
    const int mx = (int)SPL_CLAMPF(sxr0 + fu * dsx, 0.0f, (float)(srcW - 1));
    const int my = (int)SPL_CLAMPF(syr0 + fv * dsy, 0.0f, (float)(srcH - 1));
    float k = (float)src[(size_t)my * srcW + mx];

    /* RUBAN DE BORDURE 3D : la normale du biseau TOURNE avec la box, sinon le relief resterait
       éclairé comme si la box était droite. */
    if (inner_w > 0.0f && de > 0.0f) {
        const float ring = SPL_CLAMPF(inner_w - de, 0.0f, 1.0f);
        if (ring > 0.0f) {
            float rv = ringc;
            if (has_bevel) {
                const int near_x = (dyl < dxl);
                const float nlx = near_x ? 0.0f : (ul > 0.f ? 1.f : (ul < 0.f ? -1.f : 0.f));
                const float nly = near_x ? (vl > 0.f ? 1.f : (vl < 0.f ? -1.f : 0.f)) : 0.0f;
                const float gx = nlx * cvv - nly * sv;
                const float gy = nlx * sv + nly * cvv;
                const float shade = SPL_CLAMPF((lx * gx + ly * gy) * sgn * bevel_abs + lz, 0.f, 1.f);
                const float t = SPL_CLAMPF((de - pos_iw) / soft_div + 0.5f, 0.0f, 1.0f);
                const float prof = 0.35f + 0.65f * (1.0f - fabsf(2.0f * t - 1.0f));
                const float lit = SPL_CLAMPF(shade * prof * 2.0f, 0.0f, 1.6f);
                rv = SPL_CLAMPF(ringc * lit, minv, maxv);
            }
            k = k * (1.0f - ring) + rv * ring;
        }
    }

    const size_t o = (size_t)Y * dstW + X;
    dst[o] = (T)SPL_CLAMPF(dst[o] * (1.0f - a) + k * a, 0.0f, maxv);
}

extern "C" __global__ void spl_box_u8(
    unsigned char* dst, int dstW, int dstH, const unsigned char* src, int srcW, int srcH,
    int x0, int y0, int aw, int ah, int cw, int ch, float sv, float cvv, float hw, float hh,
    float fdw, float fdh, int mh, int mv, float sxr0, float dsx, float syr0, float dsy,
    int use_fe, float fe, float radf, float cref_x, float cref_y, float csoft, float cadd,
    float op, float inner_w, int has_bevel, float bevel_abs, float sgn, float pos_iw,
    float soft_div, float lx, float ly, float lz, float ringc, float minv, float maxv)
{
    spl_box_plane<unsigned char>(dst, dstW, dstH, src, srcW, srcH, x0, y0, aw, ah, cw, ch,
        sv, cvv, hw, hh, fdw, fdh, mh, mv, sxr0, dsx, syr0, dsy, use_fe, fe, radf,
        cref_x, cref_y, csoft, cadd, op, inner_w, has_bevel, bevel_abs, sgn, pos_iw,
        soft_div, lx, ly, lz, ringc, minv, maxv);
}

extern "C" __global__ void spl_box_u16(
    unsigned short* dst, int dstW, int dstH, const unsigned short* src, int srcW, int srcH,
    int x0, int y0, int aw, int ah, int cw, int ch, float sv, float cvv, float hw, float hh,
    float fdw, float fdh, int mh, int mv, float sxr0, float dsx, float syr0, float dsy,
    int use_fe, float fe, float radf, float cref_x, float cref_y, float csoft, float cadd,
    float op, float inner_w, int has_bevel, float bevel_abs, float sgn, float pos_iw,
    float soft_div, float lx, float ly, float lz, float ringc, float minv, float maxv)
{
    spl_box_plane<unsigned short>(dst, dstW, dstH, src, srcW, srcH, x0, y0, aw, ah, cw, ch,
        sv, cvv, hw, hh, fdw, fdh, mh, mv, sxr0, dsx, syr0, dsy, use_fe, fe, radf,
        cref_x, cref_y, csoft, cadd, op, inner_w, has_bevel, bevel_abs, sgn, pos_iw,
        soft_div, lx, ly, lz, ringc, minv, maxv);
}

/* ── OMBRE PORTÉE ─────────────────────────────────────────────────────────────────────────────
 * Silhouette inclinée posée dans un rect ÉLARGI (le flou doit s'épandre VERS L'EXTÉRIEUR, sinon
 * il ne fait que manger l'intérieur de la box et se coupe net au bord — ce n'est pas une ombre).
 */
extern "C" __global__ void spl_silhouette(
    float* __restrict__ sil, int sw, int sh, int pad, int aw, int ah,
    float sv, float cvv, float hw, float hh)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    if (i >= aw || j >= ah) return;
    const float xf = (float)i - (float)(aw - 1) * 0.5f;
    const float yf = (float)j - (float)(ah - 1) * 0.5f;
    const float ul = yf * sv + xf * cvv;
    const float vl = yf * cvv - xf * sv;
    const float de = fminf(hw - fabsf(ul), hh - fabsf(vl));
    sil[(size_t)(j + pad) * sw + (i + pad)] = SPL_CLAMPF(de + 0.5f, 0.0f, 1.0f);
}

/* Flou séparable, 3 box-blurs ≈ gaussienne.
 * ★ Une fenêtre glissante est SÉQUENTIELLE le long de sa ligne : un thread par ligne ne donne que
 *   quelques centaines de threads, dérisoire sur 20 SM (mesuré : 9,3 ms/trame, soit 90 % du temps
 *   GPU, pour ce seul flou). Chaque thread traite donc un SEGMENT de SPL_CHUNK pixels et
 *   recalcule sa fenêtre d'amorce — 2r additions redondantes, ~15 %, contre un parallélisme
 *   multiplié par w/SPL_CHUNK. Résultat : 9,3 → 2,2 ms. */
#define SPL_CHUNK 128

extern "C" __global__ void spl_blur_h(const float* __restrict__ in, float* __restrict__ out,
                                      int h, int w, int r)
{
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    const int c0 = (blockIdx.x * blockDim.x + threadIdx.x) * SPL_CHUNK;
    if (y >= h || c0 >= w) return;
    const float* ri = in + (size_t)y * w;
    float* ro = out + (size_t)y * w;
    const float den = (float)(2 * r);
    float acc = 0.0f;
    for (int k = max(0, c0 - r); k < min(w, c0 + r); k++) acc += ri[k];
    const int c1 = min(w, c0 + SPL_CHUNK);
    for (int c = c0; c < c1; c++) {
        if (c + r < w) acc += ri[c + r];
        if (c - r >= 0) acc -= ri[c - r];
        ro[c] = acc / den;
    }
}

extern "C" __global__ void spl_blur_v(const float* __restrict__ in, float* __restrict__ out,
                                      int h, int w, int r)
{
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y0 = (blockIdx.y * blockDim.y + threadIdx.y) * SPL_CHUNK;
    if (x >= w || y0 >= h) return;
    const float den = (float)(2 * r);
    float acc = 0.0f;
    for (int k = max(0, y0 - r); k < min(h, y0 + r); k++) acc += in[(size_t)k * w + x];
    const int y1 = min(h, y0 + SPL_CHUNK);
    for (int y = y0; y < y1; y++) {
        if (y + r < h) acc += in[(size_t)(y + r) * w + x];
        if (y - r >= 0) acc -= in[(size_t)(y - r) * w + x];
        out[(size_t)y * w + x] = acc / den;
    }
}

template <typename T>
__device__ __forceinline__ void spl_shadow_plane(
    T* __restrict__ dst, int dstW, int dstH,
    const float* __restrict__ sil, int sw, int sh, int sx0, int sy0,
    int cw, int ch, float amp, float col, float maxv)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int j = blockIdx.y * blockDim.y + threadIdx.y;
    const int pw = sw / cw, ph = sh / ch;
    if (i >= pw || j >= ph) return;
    const int X = sx0 / cw + i, Y = sy0 / ch + j;
    if (X < 0 || X >= dstW || Y < 0 || Y >= dstH) return;
    const float a = sil[(size_t)(j * ch) * sw + (i * cw)] * amp;
    if (a <= 0.0f) return;
    const size_t o = (size_t)Y * dstW + X;
    dst[o] = (T)SPL_CLAMPF(dst[o] * (1.0f - a) + col * a, 0.0f, maxv);
}

extern "C" __global__ void spl_shadow_u8(
    unsigned char* dst, int dstW, int dstH, const float* sil, int sw, int sh,
    int sx0, int sy0, int cw, int ch, float amp, float col, float maxv)
{
    spl_shadow_plane<unsigned char>(dst, dstW, dstH, sil, sw, sh, sx0, sy0, cw, ch, amp, col, maxv);
}

extern "C" __global__ void spl_shadow_u16(
    unsigned short* dst, int dstW, int dstH, const float* sil, int sw, int sh,
    int sx0, int sy0, int cw, int ch, float amp, float col, float maxv)
{
    spl_shadow_plane<unsigned short>(dst, dstW, dstH, sil, sw, sh, sx0, sy0, cw, ch, amp, col, maxv);
}
