// SPDX-License-Identifier: GPL-3.0-or-later
// Copie NON TEMPORELLE (magasins en flux) pour l'écriture d'un grain MXL.
//
// POURQUOI. Un producteur écrit une trame linéaire dans le ring et ne la relit JAMAIS. En
// écritures normales, le CPU lit d'abord chaque ligne de cache qu'il va pourtant écraser
// entièrement (read-for-ownership) : écrire 200 Mo/s génère ~400 Mo/s de trafic mémoire. Les
// magasins `movntdq`/`vmovntdq` suppriment cette lecture et ne polluent pas le L3.
//
// La glibc fait DÉJÀ cela au-delà de `glibc.cpu.x86_non_temporal_threshold` — mais ce seuil est
// dérivé du L3, donc il varie PAR NŒUD (0,94 Mo sur un R620, 18,4 Mo sur dl360-1) et une trame
// 1080p 4:2:2 8 bits (3,96 Mo) tombe SOUS le seuil sur tous nos nœuds modernes. Mesuré sur dell-1 :
// 491 → 316 µs de CPU par trame côté producteur (−35 %), 1554 → 1043 µs côté consommateur (−34 %).
//
// AU-DESSUS du seuil glibc, NE PAS utiliser ce code : la boucle non temporelle de la glibc est
// mieux réglée que celle-ci (+17 % pour la nôtre sur une trame 10 bits). L'arbitrage est fait
// côté Python (bobimxl.blit).

#include <immintrin.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

/* Variante SSE2 (movntdq, 16 o/magasin) — disponible sur TOUT x86-64, y compris les R620
   Sandy Bridge sans AVX2 (cf. le SIGILL en boucle de libmxl sur ces machines). Tête et queue non
   alignées traitées en memcpy ; `sfence` final car les magasins en flux sont faiblement ordonnés :
   sans barrière, le commit du grain pourrait devenir visible avant les octets du grain. */
void bobi_copy_nt16(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    size_t head = (16 - ((uintptr_t)dp & 15)) & 15;
    if (head > n) head = n;
    if (head) { memcpy(dp, sp, head); dp += head; sp += head; n -= head; }
    size_t nb = n & ~(size_t)63;
    for (size_t i = 0; i < nb; i += 64) {
        __m128i a = _mm_loadu_si128((const __m128i *)(sp + i));
        __m128i b = _mm_loadu_si128((const __m128i *)(sp + i + 16));
        __m128i c = _mm_loadu_si128((const __m128i *)(sp + i + 32));
        __m128i e = _mm_loadu_si128((const __m128i *)(sp + i + 48));
        _mm_stream_si128((__m128i *)(dp + i), a);
        _mm_stream_si128((__m128i *)(dp + i + 16), b);
        _mm_stream_si128((__m128i *)(dp + i + 32), c);
        _mm_stream_si128((__m128i *)(dp + i + 48), e);
    }
    if (n > nb) memcpy(dp + nb, sp + nb, n - nb);
    _mm_sfence();
}

/* Variante AVX2 (vmovntdq, 32 o/magasin, 128 o/tour). */
__attribute__((target("avx2")))
static void copy_nt32_impl(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    size_t head = (32 - ((uintptr_t)dp & 31)) & 31;
    if (head > n) head = n;
    if (head) { memcpy(dp, sp, head); dp += head; sp += head; n -= head; }
    size_t nb = n & ~(size_t)127;
    for (size_t i = 0; i < nb; i += 128) {
        __m256i a = _mm256_loadu_si256((const __m256i *)(sp + i));
        __m256i b = _mm256_loadu_si256((const __m256i *)(sp + i + 32));
        __m256i c = _mm256_loadu_si256((const __m256i *)(sp + i + 64));
        __m256i e = _mm256_loadu_si256((const __m256i *)(sp + i + 96));
        _mm256_stream_si256((__m256i *)(dp + i), a);
        _mm256_stream_si256((__m256i *)(dp + i + 32), b);
        _mm256_stream_si256((__m256i *)(dp + i + 64), c);
        _mm256_stream_si256((__m256i *)(dp + i + 96), e);
    }
    if (n > nb) memcpy(dp + nb, sp + nb, n - nb);
    _mm_sfence();
}

/* Point d'entrée unique : aiguillage À L'EXÉCUTION. Un binaire compilé avec AVX2 mais exécuté
   sur Sandy Bridge doit retomber sur la variante SSE2, pas lever SIGILL. */
void bobi_copy_nt(void *d, const void *s, size_t n)
{
    if (__builtin_cpu_supports("avx2")) copy_nt32_impl(d, s, n);
    else bobi_copy_nt16(d, s, n);
}

int bobi_nt_has_avx2(void) { return __builtin_cpu_supports("avx2") ? 1 : 0; }

/* REPLI de détection du seuil glibc quand `ld.so --list-tunables` n'est pas exploitable :
   la règle de la glibc est ≈ 3/4 du cache partagé le plus grand. Approximation suffisante — ce
   seuil ne sert qu'à décider QUI de nous ou de la glibc fait le non temporel, et les deux
   gagnent dans la zone d'incertitude. 0 si l'information est indisponible. */
size_t bobi_nt_glibc_threshold_hint(void)
{
    long l3 = sysconf(_SC_LEVEL3_CACHE_SIZE);
    if (l3 <= 0) return 0;
    return (size_t)l3 * 3 / 4;
}
