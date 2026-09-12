***REMOVED***!/usr/bin/env python3
***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Auteur : Cyril Mazouer, pour le compte de BOBI SAS
***REMOVED*** Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

***REMOVED*** Mire de DIAGNOSTIC luma/chroma : barres SMPTE qui DEFILENT horizontalement.
***REMOVED***
***REMOVED*** Pourquoi celle-ci et pas une existante : `bars` est coloree mais FIXE, `moving` bouge mais sa
***REMOVED*** chroma est NEUTRE. Or pour voir si un etage tient la chroma pendant que la luma avance, il faut
***REMOVED*** que les deux bougent ENSEMBLE et de facon connue. Ici tout le motif glisse de VITESSE px par
***REMOVED*** trame ; luma et chroma sont decalees du MEME nombre de pixels a chaque trame.
***REMOVED***
***REMOVED*** Lecture du resultat : sur la sortie de l'etage teste, on correle le profil de bords de la luma
***REMOVED*** avec celui de la chroma. Un decalage de d pixels = un ecart temporel de d/VITESSE trames.
***REMOVED*** Zero pixel = luma et chroma co-temporelles.
import sys, time, numpy as np, bobimxl

NOM     = sys.argv[1] if len(sys.argv) > 1 else "mire-couleur"
***REMOVED*** Mode INJECTION : dechire volontairement une trame sur 25 (moitie haute prise a k+1,
***REMOVED*** moitie basse a k). Sert a verifier la SENSIBILITE du veilleur — un detecteur qui ne
***REMOVED*** se declenche jamais est aussi inutile qu'un detecteur qui crie tout le temps.
DECHIRE = "--dechire" in sys.argv
W, H    = 1920, 1080
FPS     = 50
VITESSE = 16          ***REMOVED*** px par trame — 16 px a 50 fps = 800 px/s, franc et non ambigu

***REMOVED*** SMPTE 75 % : (Y, Cb, Cr) 8 bits. Barres FORTEMENT chromatiques (c'est le point).
BARRES = [(180,128,128), (162, 44,142), (131,156, 44), (112, 72, 58),
          ( 84,184,198), ( 65,100,212), ( 35,212,114), ( 16,128,128)]

def motif():
    """Une periode complete du motif, en Y (W) et en chroma (W/2), sur H lignes."""
    n  = len(BARRES)
    bw = W // n
    y  = np.zeros((H, W), np.uint8)
    u  = np.zeros((H, W // 2), np.uint8)
    v  = np.zeros((H, W // 2), np.uint8)
    for i, (yy, cb, cr) in enumerate(BARRES):
        x0, x1 = i * bw, (i + 1) * bw if i < n - 1 else W
        y[:, x0:x1] = yy
        u[:, x0 // 2:x1 // 2] = cb
        v[:, x0 // 2:x1 // 2] = cr
    return y, u, v

Y0, U0, V0 = motif()
inst = bobimxl.Instance()
w = bobimxl.Writer(inst, NOM, W, H, chroma="422", bit_depth=8, fps_num=FPS, fps_den=1)
print("mire « %s » : %dx%d %d fps, defilement %d px/trame" % (NOM, W, H, FPS, VITESSE), flush=True)

yb = W * H
ub = (W // 2) * H
k = 0
while True:
    dx  = (k * VITESSE) % W
    dxc = dx // 2
    ***REMOVED*** roll = decalage circulaire : luma et chroma glissent du MEME nombre de pixels IMAGE.
    y = np.roll(Y0, dx,  axis=1)
    u = np.roll(U0, dxc, axis=1)
    v = np.roll(V0, dxc, axis=1)
    if DECHIRE and k % 25 == 0:
        dx2, dxc2 = ((k + 1) * VITESSE) % W, (((k + 1) * VITESSE) % W) // 2
        y[:H // 2] = np.roll(Y0, dx2,  axis=1)[:H // 2]
        u[:H // 2] = np.roll(U0, dxc2, axis=1)[:H // 2]
        v[:H // 2] = np.roll(V0, dxc2, axis=1)[:H // 2]
    idx, gi, vue = w.open_grain()
    vue[:yb]           = y.reshape(-1)
    vue[yb:yb + ub]    = u.reshape(-1)
    vue[yb + ub:yb + 2 * ub] = v.reshape(-1)
    w.commit(gi)
    k += 1
    time.sleep(1.0 / FPS)
