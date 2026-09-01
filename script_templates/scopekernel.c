/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * Noyaux de mesure du plugin `scope` : waveform et vecteur-scope.
 *
 * POURQUOI EN C. Mesuré le 2026-08-25 (cf. docs/chantiers/SCOPE.md §3) : en numpy, une trame 1080
 * coûte 12 à 18 ms selon la taille de l'histogramme — hors budget dès qu'on ajoute le rendu. Le
 * même calcul en C tient en 2,63 ms sur un cœur Cascade Lake. L'écart ne vient pas de numpy : il
 * vient de la construction d'un tableau d'index de 2 M d'entiers, que la version C n'a pas besoin
 * de matérialiser.
 *
 * CE QUI COMMANDE LE COÛT, et ce n'est pas la fréquence : c'est le CACHE. Mesuré, toujours en 1080 :
 *   491 520 cases (1920 colonnes) → 6,88 ms   (1,97 Mo : déborde le L2, travaille en L3)
 *   131 072 cases ( 512 colonnes) → 2,63 ms   (512 Ko : tient en L2)
 * D'où le choix de 512 colonnes. Ce n'est pas un compromis d'affichage — un waveform se dessine sur
 * 500 à 800 pixels de large, personne n'affiche 1920 colonnes distinctes.
 *
 * ON GARDE TOUTES LES LIGNES. Décimer ferait rater l'événement rare — la ligne unique hors norme,
 * le sous-titre trop lumineux, l'éclair sur une ligne — c'est-à-dire tout l'intérêt de l'appareil.
 * Le budget le permet largement, il n'y a donc aucune raison d'y renoncer.
 *
 * PAS DE `-march=native`, ET C'EST DÉLIBÉRÉ. La boucle est bornée par des écritures DISPERSÉES
 * (`hist[colonne * niveaux + valeur]++`) : elle ne se vectorise pas, le SIMD n'apporterait rien.
 * Compilé en variantes x86-64-v2 / v3 comme les autres noyaux du parc, cf. le Dockerfile de
 * `_compute_runtime` — un `.so` bâti pour une micro-architecture plus riche que le nœud cible
 * lève SIGILL, on s'en est fait piéger le 2026-08-22 en transportant une image Cascade Lake vers
 * un Broadwell.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* ── Waveform : luminance en fonction de la position horizontale ────────────────────────────────
 * `colbin[c]` donne la colonne d'histogramme du pixel c — précalculé côté Python une fois pour
 * toutes, ce qui évite une division par pixel dans la boucle chaude.
 * `hist` est un tableau [colonnes][niveaux] que l'appelant a mis à zéro (ou pas : cf. `remise_a_zero`).
 */
void bobi_waveform(const uint16_t *y, int hauteur, int largeur, int pas_ligne,
                   int niveaux, int decalage, const uint16_t *colbin, uint32_t *hist) {
  for (int r = 0; r < hauteur; r++) {
    const uint16_t *ligne = y + (size_t)r * (size_t)pas_ligne;
    for (int c = 0; c < largeur; c++) {
      uint32_t v = (uint32_t)(ligne[c] >> decalage);
      hist[(size_t)colbin[c] * (size_t)niveaux + v]++;
    }
  }
}

/* ── Vecteur-scope : nuage Cb/Cr ────────────────────────────────────────────────────────────────
 * Les plans de chrominance sont sous-échantillonnés (4:2:2 → moitié moins de colonnes), donc deux
 * fois moins d'échantillons que la luma. `cote` est la résolution du nuage (256 → 65 536 cases,
 * largement en L2).
 */
void bobi_vecteur(const uint16_t *cb, const uint16_t *cr, int hauteur, int largeur,
                  int pas_ligne, int cote, int decalage, uint32_t *hist) {
  for (int r = 0; r < hauteur; r++) {
    const uint16_t *lb = cb + (size_t)r * (size_t)pas_ligne;
    const uint16_t *lr = cr + (size_t)r * (size_t)pas_ligne;
    for (int c = 0; c < largeur; c++) {
      uint32_t u = (uint32_t)(lb[c] >> decalage);
      uint32_t v = (uint32_t)(lr[c] >> decalage);
      hist[(size_t)v * (size_t)cote + u]++;
    }
  }
}

/* ── Extrêmes et dépassements ───────────────────────────────────────────────────────────────────
 * SÉPARÉ du waveform, et sur TOUTE la trame sans exception : le waveform est binné (256 niveaux
 * pour 1024 valeurs), il ne peut donc pas dire si un pixel a réellement franchi la limite légale.
 * Cette passe-là est la seule qui fasse foi pour une alarme.
 *
 * `bas` / `haut` = bornes légales dans l'échelle native du flux (64 et 940 en 10 bits vidéo).
 * `res[0..3]` = min, max, nombre de pixels sous la borne, nombre au-dessus.
 */
void bobi_extremes(const uint16_t *y, int hauteur, int largeur, int pas_ligne,
                   uint32_t bas, uint32_t haut, uint32_t *res) {
  uint32_t mini = 0xFFFFFFFFu, maxi = 0, sous = 0, sur = 0;
  for (int r = 0; r < hauteur; r++) {
    const uint16_t *ligne = y + (size_t)r * (size_t)pas_ligne;
    for (int c = 0; c < largeur; c++) {
      uint32_t v = ligne[c];
      if (v < mini) mini = v;
      if (v > maxi) maxi = v;
      if (v < bas)  sous++;
      if (v > haut) sur++;
    }
  }
  res[0] = mini; res[1] = maxi; res[2] = sous; res[3] = sur;
}

/* ── Waveform R/V/B : convertir ET binner, UN PLAN À LA FOIS ─────────────────────────────────
 * MESURÉ le 2026-08-25, et deux fois plutôt qu'une :
 *   · la même conversion en numpy coûte 71 ms sur une trame 1080 — trois fois et demie le
 *     budget d'une trame — parce qu'elle matérialise trois plans de 2 M pixels avant de binner ;
 *   · une première version en C qui remplissait les TROIS histogrammes dans la même passe
 *     coûtait encore 52 ms. Trois histogrammes de 512 Ko font 1,5 Mo de cases dispersées : très
 *     au-delà du L2. C'est exactement la leçon du binnage du waveform (cf. l'en-tête de ce
 *     fichier), et elle se paie deux fois si on l'oublie.
 * D'où : UNE PASSE PAR PLAN, chacune avec un seul histogramme qui tient en L2. On relit les
 * sources trois fois, mais séquentiellement — le préchargeur matériel fait ça très bien, alors
 * qu'il ne peut rien pour des écritures dispersées.
 *
 * `dx`/`dy` sont des DÉCALAGES, pas des facteurs : une division entière par pixel sur 2 M
 * pixels se voit au chronomètre. 4:2:2 → (1,0) ; 4:2:0 → (1,1) ; 4:4:4 → (0,0).
 *
 * LES COEFFICIENTS VIENNENT DE L'APPELANT, en virgule fixe 16.16. C'est délibéré et ce n'est
 * pas un détail : la matrice dépend de la colorimétrie DÉCLARÉE par le flux (BT.709 ≠ BT.601
 * ≠ BT.2020). En coder une ici donnerait des tracés faux sur une source sur deux, sans que
 * rien ne le signale — le genre de faute qu'on ne découvre qu'en comparant à un autre
 * appareil. Le script les calcule à partir de (Kr, Kb) et REFUSE le mode si la colorimétrie
 * n'est pas déclarée.
 *
 * ÉCHELLE D'ÉTUDE CONSERVÉE : R'V'B' reste dans la même plage que Y' (16-235 en 8 bits), ce
 * qui est la convention des scopes de production — le même réticule sert pour la luma et pour
 * la parade, et 100 % veut dire la même chose sur les deux. D'où des coefficients déjà
 * multipliés par 219/224 côté appelant.
 *
 * `plan` : 0 = R, 1 = V, 2 = B. `coef` = {r_cr, g_cb, g_cr, b_cb} en 16.16.
 */
void bobi_waveform_rgb(const uint16_t *y, const uint16_t *cb, const uint16_t *cr,
                       int hauteur, int largeur, int pas_y, int pas_c, int dx, int dy,
                       int niveaux, int decalage, const uint16_t *colbin,
                       const int32_t *coef, int offset_c, int maxval,
                       int plan, uint32_t *hist) {
  const int32_t a = (plan == 0) ? 0 : (plan == 1) ? coef[1] : coef[3];   /* poids de Cb */
  const int32_t b = (plan == 0) ? coef[0] : (plan == 1) ? coef[2] : 0;   /* poids de Cr */
  const int signe = (plan == 1) ? -1 : 1;
  for (int r = 0; r < hauteur; r++) {
    const uint16_t *ly = y  + (size_t)r * (size_t)pas_y;
    const uint16_t *lb = cb + (size_t)(r >> dy) * (size_t)pas_c;
    const uint16_t *lr = cr + (size_t)(r >> dy) * (size_t)pas_c;
    for (int c = 0; c < largeur; c++) {
      const int ci = c >> dx;
      int32_t val = (int32_t)ly[c]
                  + signe * ((a * ((int32_t)lb[ci] - offset_c)) >> 16)
                  + signe * ((b * ((int32_t)lr[ci] - offset_c)) >> 16);
      /* Bornage AVANT binnage : un dépassement négatif décalerait l'index et irait écrire dans
       * une autre colonne — une corruption silencieuse, pas un pixel faux.
       * SANS BRANCHE : sur un contenu très saturé, le débordement est imprévisible et une
       * branche mal prédite coûte plus cher que tout le calcul du pixel. */
      val &= ~(val >> 31);                          /* négatif → 0 */
      val = val > maxval ? maxval : val;            /* gcc émet un cmov */
      hist[(size_t)colbin[c] * (size_t)niveaux + (uint32_t)(val >> decalage)]++;
    }
  }
}

/* ── Filtre K de l'ITU-R BS.1770 + somme des carrés ──────────────────────────────────────────
 * Un IIR est SÉQUENTIEL par nature : chaque échantillon dépend des précédents. Impossible à
 * vectoriser en numpy, et une boucle Python sur 48 000 échantillons par seconde et par canal
 * est hors de question. D'où ce noyau.
 *
 * LES COEFFICIENTS VIENNENT DE L'APPELANT, comme pour la matrice R/V/B, et pour la même raison :
 * ils dépendent de la CADENCE D'ÉCHANTILLONNAGE réelle du flux. Une table figée à 48 kHz — c'est
 * celle que publie la norme — donnerait un filtre faux sur du 44,1 ou du 96. Le script les
 * dérive du gabarit normalisé et VÉRIFIE, à 48 kHz, qu'il retrouve la table publiée.
 *
 * On ne rend PAS le signal filtré : R128 n'a besoin que de la somme des carrés par canal. La
 * matérialiser coûterait une copie de la taille du bloc pour rien.
 *
 * `x` : entrelacé (n, ch), float32. `etat` : 8 doubles par canal (x1,x2,y1,y2 de chaque étage),
 * conservé d'un appel à l'autre — le filtre ne doit PAS repartir de zéro à chaque bloc, sinon
 * chaque frontière de bloc injecte un transitoire dans la mesure. `ss` est ACCUMULÉ, pas remis
 * à zéro : l'appelant décide de la fenêtre.
 */
void bobi_kweight_ss(const float *x, int n, int ch,
                     const double *c1, const double *c2,
                     double *etat, double *ss) {
  for (int c = 0; c < ch; c++) {
    double *e = etat + (size_t)c * 8;
    double x1 = e[0], x2 = e[1], y1 = e[2], y2 = e[3];   /* étage 1 */
    double u1 = e[4], u2 = e[5], v1 = e[6], v2 = e[7];   /* étage 2 */
    double acc = 0.0;
    for (int i = 0; i < n; i++) {
      const double s = (double)x[(size_t)i * (size_t)ch + (size_t)c];
      double y = c1[0]*s + c1[1]*x1 + c1[2]*x2 - c1[3]*y1 - c1[4]*y2;
      x2 = x1; x1 = s; y2 = y1; y1 = y;
      double v = c2[0]*y + c2[1]*u1 + c2[2]*u2 - c2[3]*v1 - c2[4]*v2;
      u2 = u1; u1 = y; v2 = v1; v1 = v;
      acc += v * v;
    }
    e[0] = x1; e[1] = x2; e[2] = y1; e[3] = y2;
    e[4] = u1; e[5] = u2; e[6] = v1; e[7] = v2;
    ss[c] += acc;
  }
}

/* ── Crête et valeur efficace par canal, sur un bloc entrelacé ───────────────────────────────
 * Sépare du filtre K : les bar-graphs se lisent sur le signal BRUT (ce que porte le flux), pas
 * sur un signal pondéré pour la perception. Les confondre donnerait des mètres qui ne
 * correspondent à aucun autre appareil.
 * `crete` est un MAXIMUM accumulé (maintien), `ss` une somme de carrés accumulée.
 */
void bobi_niveaux(const float *x, int n, int ch, double *crete, double *ss) {
  for (int c = 0; c < ch; c++) {
    double mx = crete[c], acc = 0.0;
    for (int i = 0; i < n; i++) {
      const double s = (double)x[(size_t)i * (size_t)ch + (size_t)c];
      const double a = s < 0 ? -s : s;
      if (a > mx) mx = a;
      acc += s * s;
    }
    crete[c] = mx; ss[c] += acc;
  }
}

/* ── Gamut RVB : compter les dépassements ET binner les deux losanges, EN UNE PASSE ──────────
 * ★ POURQUOI CE NOYAU EXISTE. En numpy, le comptage exact coûte 22 ms sur 1920×1080 — plus que
 * le budget d'une trame à 50 i/s. La mesure avait donc été bridée à 1 Hz, c'est-à-dire à UNE
 * TRAME SUR CINQUANTE : un dépassement de trois trames avait 94 % de chances de passer
 * inaperçu, pendant que le waveform, lui, voyait tout. C'est la décimation que ce fichier
 * refuse dans l'espace, commise dans le temps.
 *
 * ★ ET LE NUAGE SE FAIT DANS LA MÊME PASSE. Le calcul cher est la matrice par pixel ; une fois
 * R'V'B' en main, binner coûte deux incréments. Deux passes séparées paieraient deux fois la
 * conversion pour rien — c'est la leçon déjà apprise sur `bobi_waveform_rgb`, dont chaque plan
 * refait la lecture.
 *
 * LES COEFFICIENTS VIENNENT DE L'APPELANT, en 16.16, exactement comme pour la parade et pour
 * la même raison : la matrice dépend de la colorimétrie DÉCLARÉE. En coder une ici donnerait
 * un pourcentage faux sur une source sur deux, et faux EN SILENCE — la faute que la sonde du
 * moteur a commise pendant des mois.
 *
 * GÉOMÉTRIE DU DIAGRAMME, en unités de code (0 = noir d'étude, `plage` = blanc) :
 *     u = a − V      v = a + V − plage      losange ⟺ |u| + |v| ≤ plage
 * Les huit sommets du cube R'V'B' tombent exactement sur le bord : le CONTOUR EST LE SEUIL, il
 * n'y a aucune tolérance visuelle à régler. Le neutre (R=V=B) a u = 0, donc l'axe des gris est
 * la verticale centrale — par construction, pas par réglage.
 *
 * `tol` : tolérance de COMPTAGE en 16.16 de la plage (0.02 → 1311). Elle élargit ce qu'on
 * compte comme dépassement, JAMAIS le tracé.
 * `nuage` : `cote`×`cote` en uint32, ou NULL pour ne compter que. `res[0]` = pixels hors
 * gamut, `res[1]` = pixels examinés.
 */
void bobi_gamut(const uint16_t *y, const uint16_t *cb, const uint16_t *cr,
                int hauteur, int largeur, int pas_y, int pas_c, int dx, int dy,
                const int32_t *coef, int offset_c, int noir, int plage, int32_t tol,
                int cote, uint32_t *nuage, uint64_t *res) {
  const int32_t bas  = -(int32_t)(((int64_t)tol * plage) >> 16);
  const int32_t haut = plage - bas;                 /* plage + tolérance */
  const int demi = cote >> 1, ray = cote >> 2;
  /* Réciproque de la plage en 16.16 : une division par pixel coûterait plus que la matrice. */
  const int32_t inv = plage > 0 ? (int32_t)(((int64_t)ray << 16) / plage) : 0;
  const int cy_h = ray, cy_b = cote - ray - 1;
  uint64_t hors = 0, total = 0;
  for (int r = 0; r < hauteur; r++) {
    const uint16_t *ly = y  + (size_t)r * (size_t)pas_y;
    const uint16_t *lb = cb + (size_t)(r >> dy) * (size_t)pas_c;
    const uint16_t *lr = cr + (size_t)(r >> dy) * (size_t)pas_c;
    for (int c = 0; c < largeur; c++) {
      const int ci = c >> dx;
      const int32_t yn = (int32_t)ly[c] - noir;
      const int32_t ub = (int32_t)lb[ci] - offset_c;
      const int32_t ur = (int32_t)lr[ci] - offset_c;
      /* ⚠ MULTIPLICATIONS 32 BITS, PAS 64. Le premier jet passait par `int64_t` par prudence :
       * 16,3 ms au lieu de 8,4 sur 1920×1080, parce qu'une multiplication 64 bits ne se
       * vectorise pas. La marge est pourtant confortable — le plus gros coefficient vaut
       * ~101 000 (17 bits) et l'écart de chroma ±512 en 10 bits (10 bits) : 27 bits de
       * produit, très loin des 31 disponibles. Même en 12 bits on reste sous les 30. */
      const int32_t R = yn + ((coef[0] * ur) >> 16);
      const int32_t V = yn - ((coef[1] * ub) >> 16) - ((coef[2] * ur) >> 16);
      const int32_t B = yn + ((coef[3] * ub) >> 16);
      /* SANS BRANCHE : sur un contenu saturé le résultat est imprévisible, et une branche mal
       * prédite coûte plus cher que la matrice elle-même. */
      hors += (uint64_t)((R < bas) | (R > haut) | (V < bas) | (V > haut)
                       | (B < bas) | (B > haut));
      total++;
      if (nuage) {
        /* Losange du HAUT : (V, B). Losange du BAS : (V, R). */
        int32_t u = B - V, v = B + V - plage;
        int ix = demi + ((u * inv) >> 16);
        int iy = cy_h - ((v * inv) >> 16);
        if (ix < 0) ix = 0; else if (ix >= cote) ix = cote - 1;
        if (iy < 0) iy = 0; else if (iy >= cote) iy = cote - 1;
        nuage[(size_t)iy * (size_t)cote + (size_t)ix]++;
        u = R - V; v = R + V - plage;
        ix = demi + ((u * inv) >> 16);
        iy = cy_b + ((v * inv) >> 16);
        if (ix < 0) ix = 0; else if (ix >= cote) ix = cote - 1;
        if (iy < 0) iy = 0; else if (iy >= cote) iy = cote - 1;
        nuage[(size_t)iy * (size_t)cote + (size_t)ix]++;
      }
    }
  }
  res[0] = hors; res[1] = total;
}
