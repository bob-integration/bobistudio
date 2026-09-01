/*
 * v210convert — pack/unpack v210 (4:2:2 10 bits, SMPTE/QuickTime) ↔ planar maison.
 *
 * Chantier interop MXL (MXL_INTEROP.md §Mise à jour 2026-07-12) : le SDK MXL stock ne
 * parle que video/v210 ; notre bus interne est planar (x-mxl-planar, fork). Ce module
 * fournit la conversion aux frontières (pont R1) — et, si le tout-v210 (R3) est acté un
 * jour, le même code sert de convertisseur transparent dans bobimxl.
 *
 * Écrit en C scalaire simple, auto-vectorisé par gcc -O3 -march=native (mesuré ~4-5 ms
 * l'aller-retour 1080p sur Xeon 6240R, vs ~33 ms en numpy — cf. mémoire
 * planar-v210-cpu-measure). Le layout planar maison = plans Y, Cb, Cr contigus
 * (cf. bobimxl.frame_bytes) ; 8 bits = 1 o/sample, 10 bits = PLANAR10LE (uint16 LE).
 *
 * Layout v210 (bit-exact FFmpeg/Apple) : la ligne est une suite de groupes de 6 pixels
 * sur 16 octets = 4 mots u32 little-endian, 3 échantillons 10 bits par mot
 * (bits [9:0], [19:10], [29:20]) :
 *   w0: Cb0 Y0 Cr0   w1: Y1 Cb2 Y2   w2: Cr2 Y3 Cb4   w3: Y4 Cr4 Y5
 * Stride ligne = ((width + 47) / 48) * 128 octets (aligné 48 px / 128 o), padding à 0.
 *
 * Build : gcc -O3 -march=native -shared -fPIC v210convert.c -o libbobi_v210.so
 * (fait dans plugins/_compute_runtime/Dockerfile ; binding ctypes dans bobimxl.py).
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))

EXPORT size_t bobi_v210_stride(int width)
{
    return (size_t)((width + 47) / 48) * 128;
}

/* --- helpers 1 groupe (6 px) --------------------------------------------------------- */

static inline void unpack_group(const uint32_t *w, uint16_t *y, uint16_t *cb, uint16_t *cr)
{
    uint32_t w0 = w[0], w1 = w[1], w2 = w[2], w3 = w[3];
    cb[0] =  w0        & 0x3ff;
    y[0]  = (w0 >> 10) & 0x3ff;
    cr[0] = (w0 >> 20) & 0x3ff;
    y[1]  =  w1        & 0x3ff;
    cb[1] = (w1 >> 10) & 0x3ff;
    y[2]  = (w1 >> 20) & 0x3ff;
    cr[1] =  w2        & 0x3ff;
    y[3]  = (w2 >> 10) & 0x3ff;
    cb[2] = (w2 >> 20) & 0x3ff;
    y[4]  =  w3        & 0x3ff;
    cr[2] = (w3 >> 10) & 0x3ff;
    y[5]  = (w3 >> 20) & 0x3ff;
}

static inline void pack_group(uint32_t *w, const uint16_t *y, const uint16_t *cb,
                              const uint16_t *cr)
{
    w[0] = (uint32_t)(cb[0] & 0x3ff) | ((uint32_t)(y[0]  & 0x3ff) << 10)
                                     | ((uint32_t)(cr[0] & 0x3ff) << 20);
    w[1] = (uint32_t)(y[1]  & 0x3ff) | ((uint32_t)(cb[1] & 0x3ff) << 10)
                                     | ((uint32_t)(y[2]  & 0x3ff) << 20);
    w[2] = (uint32_t)(cr[1] & 0x3ff) | ((uint32_t)(y[3]  & 0x3ff) << 10)
                                     | ((uint32_t)(cb[2] & 0x3ff) << 20);
    w[3] = (uint32_t)(y[4]  & 0x3ff) | ((uint32_t)(cr[2] & 0x3ff) << 10)
                                     | ((uint32_t)(y[5]  & 0x3ff) << 20);
}

/* --- 10 bits : v210 ↔ PLANAR10LE (uint16/sample) ------------------------------------- */

EXPORT void bobi_v210_unpack10(const uint8_t *restrict src, size_t src_stride,
                               uint16_t *restrict y, uint16_t *restrict cb,
                               uint16_t *restrict cr, int width, int height)
{
    int ngrp = width / 6, rem = width % 6, cw = width / 2;
    for (int r = 0; r < height; r++) {
        const uint32_t *w = (const uint32_t *)(src + (size_t)r * src_stride);
        uint16_t *yr = y + (size_t)r * width;
        uint16_t *cbr = cb + (size_t)r * cw;
        uint16_t *crr = cr + (size_t)r * cw;
        for (int g = 0; g < ngrp; g++)
            unpack_group(w + 4 * (size_t)g, yr + 6 * (size_t)g,
                         cbr + 3 * (size_t)g, crr + 3 * (size_t)g);
        if (rem) {                      /* queue < 6 px : groupe décodé à part, copie partielle */
            uint16_t ty[6], tcb[3], tcr[3];
            unpack_group(w + 4 * (size_t)ngrp, ty, tcb, tcr);
            memcpy(yr + 6 * (size_t)ngrp, ty, (size_t)rem * 2);
            memcpy(cbr + 3 * (size_t)ngrp, tcb, (size_t)((rem + 1) / 2) * 2);
            memcpy(crr + 3 * (size_t)ngrp, tcr, (size_t)((rem + 1) / 2) * 2);
        }
    }
}

EXPORT void bobi_v210_pack10(const uint16_t *restrict y, const uint16_t *restrict cb,
                             const uint16_t *restrict cr, uint8_t *restrict dst,
                             size_t dst_stride, int width, int height)
{
    int ngrp = width / 6, rem = width % 6, cw = width / 2;
    size_t used = 16 * (size_t)(ngrp + (rem ? 1 : 0));
    for (int r = 0; r < height; r++) {
        uint32_t *w = (uint32_t *)(dst + (size_t)r * dst_stride);
        const uint16_t *yr = y + (size_t)r * width;
        const uint16_t *cbr = cb + (size_t)r * cw;
        const uint16_t *crr = cr + (size_t)r * cw;
        for (int g = 0; g < ngrp; g++)
            pack_group(w + 4 * (size_t)g, yr + 6 * (size_t)g,
                       cbr + 3 * (size_t)g, crr + 3 * (size_t)g);
        if (rem) {                      /* queue : groupe complété par réplication du dernier px */
            uint16_t ty[6], tcb[3], tcr[3];
            for (int i = 0; i < 6; i++) ty[i] = yr[6 * ngrp + (i < rem ? i : rem - 1)];
            int crem = (rem + 1) / 2;
            for (int i = 0; i < 3; i++) {
                tcb[i] = cbr[3 * ngrp + (i < crem ? i : crem - 1)];
                tcr[i] = crr[3 * ngrp + (i < crem ? i : crem - 1)];
            }
            pack_group(w + 4 * (size_t)ngrp, ty, tcb, tcr);
        }
        if (used < dst_stride)          /* padding d'alignement 128 o : à zéro (spec) */
            memset((uint8_t *)w + used, 0, dst_stride - used);
    }
}

/* --- 8 bits : v210 ↔ planar8 (pipeline force8 : v>>2 à l'unpack, v<<2 au pack) -------- */

EXPORT void bobi_v210_unpack8(const uint8_t *restrict src, size_t src_stride,
                              uint8_t *restrict y, uint8_t *restrict cb,
                              uint8_t *restrict cr, int width, int height)
{
    int ngrp = width / 6, rem = width % 6, cw = width / 2;
    for (int r = 0; r < height; r++) {
        const uint32_t *w = (const uint32_t *)(src + (size_t)r * src_stride);
        uint8_t *yr = y + (size_t)r * width;
        uint8_t *cbr = cb + (size_t)r * cw;
        uint8_t *crr = cr + (size_t)r * cw;
        for (int g = 0; g < ngrp; g++) {
            uint32_t w0 = w[4*g], w1 = w[4*g+1], w2 = w[4*g+2], w3 = w[4*g+3];
            cbr[3*g]   = (uint8_t)((w0 >>  2) & 0xff);
            yr[6*g]    = (uint8_t)((w0 >> 12) & 0xff);
            crr[3*g]   = (uint8_t)((w0 >> 22) & 0xff);
            yr[6*g+1]  = (uint8_t)((w1 >>  2) & 0xff);
            cbr[3*g+1] = (uint8_t)((w1 >> 12) & 0xff);
            yr[6*g+2]  = (uint8_t)((w1 >> 22) & 0xff);
            crr[3*g+1] = (uint8_t)((w2 >>  2) & 0xff);
            yr[6*g+3]  = (uint8_t)((w2 >> 12) & 0xff);
            cbr[3*g+2] = (uint8_t)((w2 >> 22) & 0xff);
            yr[6*g+4]  = (uint8_t)((w3 >>  2) & 0xff);
            crr[3*g+2] = (uint8_t)((w3 >> 12) & 0xff);
            yr[6*g+5]  = (uint8_t)((w3 >> 22) & 0xff);
        }
        if (rem) {
            uint16_t ty[6], tcb[3], tcr[3];
            unpack_group(w + 4 * (size_t)ngrp, ty, tcb, tcr);
            for (int i = 0; i < rem; i++) yr[6 * ngrp + i] = (uint8_t)(ty[i] >> 2);
            for (int i = 0; i < (rem + 1) / 2; i++) {
                cbr[3 * ngrp + i] = (uint8_t)(tcb[i] >> 2);
                crr[3 * ngrp + i] = (uint8_t)(tcr[i] >> 2);
            }
        }
    }
}

EXPORT void bobi_v210_pack8(const uint8_t *restrict y, const uint8_t *restrict cb,
                            const uint8_t *restrict cr, uint8_t *restrict dst,
                            size_t dst_stride, int width, int height)
{
    int ngrp = width / 6, rem = width % 6, cw = width / 2;
    size_t used = 16 * (size_t)(ngrp + (rem ? 1 : 0));
    for (int r = 0; r < height; r++) {
        uint32_t *w = (uint32_t *)(dst + (size_t)r * dst_stride);
        const uint8_t *yr = y + (size_t)r * width;
        const uint8_t *cbr = cb + (size_t)r * cw;
        const uint8_t *crr = cr + (size_t)r * cw;
        for (int g = 0; g < ngrp; g++) {
            w[4*g]   = ((uint32_t)cbr[3*g]   << 2) | ((uint32_t)yr[6*g]    << 12)
                                                   | ((uint32_t)crr[3*g]   << 22);
            w[4*g+1] = ((uint32_t)yr[6*g+1]  << 2) | ((uint32_t)cbr[3*g+1] << 12)
                                                   | ((uint32_t)yr[6*g+2]  << 22);
            w[4*g+2] = ((uint32_t)crr[3*g+1] << 2) | ((uint32_t)yr[6*g+3]  << 12)
                                                   | ((uint32_t)cbr[3*g+2] << 22);
            w[4*g+3] = ((uint32_t)yr[6*g+4]  << 2) | ((uint32_t)crr[3*g+2] << 12)
                                                   | ((uint32_t)yr[6*g+5]  << 22);
        }
        if (rem) {
            uint16_t ty[6], tcb[3], tcr[3];
            for (int i = 0; i < 6; i++)
                ty[i] = (uint16_t)yr[6 * ngrp + (i < rem ? i : rem - 1)] << 2;
            int crem = (rem + 1) / 2;
            for (int i = 0; i < 3; i++) {
                tcb[i] = (uint16_t)cbr[3 * ngrp + (i < crem ? i : crem - 1)] << 2;
                tcr[i] = (uint16_t)crr[3 * ngrp + (i < crem ? i : crem - 1)] << 2;
            }
            pack_group(w + 4 * (size_t)ngrp, ty, tcb, tcr);
        }
        if (used < dst_stride)
            memset((uint8_t *)w + used, 0, dst_stride - used);
    }
}
