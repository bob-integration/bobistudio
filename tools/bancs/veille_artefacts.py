#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# Veilleur d'artefacts d'image sur des flux MXL — attrape l'evenement au lieu de le deduire.
#
# Pourquoi : le defaut signale est INTERMITTENT. Des captures d'une demi-seconde analysees apres
# coup ne le voient pas, et chercher a le prouver dessus mene a inventer des metriques qui disent
# ce qu'on cherche (vecu le 2026-08-13). On surveille donc en continu et on JOURNALISE l'evenement.
#
# Deux defauts guettes, chacun avec un TEMOIN de reference :
#
#   1. DECALAGE LUMA/CHROMA — correlation des profils de bords horizontaux de Y et de la chroma.
#      Un ecart de d colonnes chroma = les deux plans viennent d'instants differents. Sur une
#      chaine saine la mesure sort 0 avec un ecart-type nul (verifie sur mire a barres defilantes).
#      N'est significatif que si la correlation est FRANCHE (sinon l'image n'a pas assez de bords
#      chromatiques pour conclure — on s'abstient plutot que de rapporter du bruit).
#
#   2. DECHIRURE PAR BANDES — un meme grain portant des bandes de trames differentes.
#      ⚠ NE PAS confondre avec « des bandes bougent, d'autres non » : c'est la description d'un
#      sujet mobile sur fond fixe, donc de TOUTE video normale (premiere version de ce veilleur,
#      qui flaggait chaque trame). Le vrai critere est temporel : une bande DECHIREE porte deja
#      la trame SUIVANTE (elle est arrivee en avance) pendant que ses voisines portent encore la
#      courante. On compare donc chaque bande a la trame precedente ET a la suivante, et on
#      n'examine que les bandes reellement ACTIVES — une bande immobile ne dit rien.
#
# Sortie : une ligne par evenement dans le journal + la trame fautive en PNG (bornee en nombre).
import sys, os, time, json
import numpy as np
import bobimxl

FLUX     = sys.argv[1].split(",")
DUREE    = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0
SORTIE   = sys.argv[3] if len(sys.argv) > 3 else "/tmp/veille"
MAX_PNG  = 40                 # borne dure : un veilleur ne doit pas remplir le disque
BANDES   = 12                 # decoupe horizontale pour la detection de dechirure

os.makedirs(SORTIE, exist_ok=True)
JOURNAL = open(os.path.join(SORTIE, "veille.log"), "a", buffering=1)

def dire(msg):
    ligne = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
    print(ligne, flush=True)
    JOURNAL.write(ligne + "\n")

def profil(p):
    e = np.abs(np.diff(p, axis=1)).mean(0)
    s = e.std()
    return (e - e.mean()) / (s + 1e-9), float(s)

def decalage(Y, U, V):
    """(lag en colonnes chroma, force de la correlation). lag=None si l'image n'a pas assez de
    bords chromatiques pour conclure — l'abstention est un resultat, pas un echec."""
    a, sa = profil(Y)
    bu, su = profil(U); bv, sv = profil(V)
    # ⚠ Seuil d'ABSTENTION. A 1.0 il laissait passer des scenes quasi monochromes (facade grise
    # d'un plan de ville) : la correlation portait alors sur du bruit et sortait « +8 colonnes,
    # corr 0.53 » 147 fois de suite — un faux positif franc, verifie a l'image le 2026-08-13.
    # 4.0 exige de vrais bords COLORES avant de conclure quoi que ce soit.
    if sa < 1.0 or (su + sv) < 4.0:
        return None, 0.0
    b = bu + bv; b = (b - b.mean()) / (b.std() + 1e-9)
    best, arg = -9.0, 0
    for s in range(-40, 41):
        c = float((a[s:] * b[:len(b) - s]).mean()) if s >= 0 else float((a[:len(a) + s] * b[-s:]).mean())
        if c > best: best, arg = c, s
    return arg, best

def dechirure(Yprec, Y, Ysuiv):
    """(indice_de_frontiere, ecart) si le grain melange des trames, sinon (None, 0).

    Pour chaque bande ACTIVE (qui change vraiment), r = d(N,N+1) / (d(N,N-1) + d(N,N+1)) :
      r ~ 0,5  la bande evolue normalement dans les deux sens ;
      r ~ 0    elle est DEJA identique a la suivante — elle a pris de l'avance ;
      r ~ 1    elle est encore identique a la precedente — elle est en retard.
    Une trame saine donne un r homogene ; une dechirure donne deux populations separees par une
    frontiere nette. Les bandes immobiles sont ECARTEES : elles n'ont pas d'opinion."""
    h = Y.shape[0] // BANDES
    r, actives = [], []
    for i in range(BANDES):
        a = Y[i * h:(i + 1) * h]
        dp = float(np.abs(a - Yprec[i * h:(i + 1) * h]).mean())
        ds = float(np.abs(a - Ysuiv[i * h:(i + 1) * h]).mean())
        if max(dp, ds) < 2.0:
            r.append(None); continue          # bande immobile : sans opinion
        r.append(ds / (dp + ds + 1e-9)); actives.append(i)
    if len(actives) < 4:
        return None, 0.0                       # trop peu de matiere pour conclure
    vals = np.array([r[i] for i in actives])
    # ⚠ PLANCHER CONNU : un FONDU ENCHAINE produit un large ecart de r sans aucune dechirure
    # (verifie a l'image le 2026-08-14). Il vaut ~3 evenements/min sur du contenu a transitions.
    # Une tentative de le supprimer en exigeant la bimodalite (min<0.20 et max>0.60) a SUPPRIME
    # AUSSI la detection des vraies dechirures : calibre sur mire volontairement cassee, une
    # vraie dechirure donne min=0.50 max=1.00, ecart 0.50 — soit juste au seuil. Ce detecteur
    # sait donc distinguer 18/min de 5/min, PAS 5 de 3. Ne pas lui faire dire plus que ca.
    if vals.max() - vals.min() < 0.55:
        return None, float(vals.max() - vals.min())
    # frontiere = la transition la plus franche entre bandes actives VOISINES
    saut, frontiere = 0.0, None
    for j in range(len(actives) - 1):
        if actives[j + 1] == actives[j] + 1:
            e = abs(vals[j + 1] - vals[j])
            if e > saut: saut, frontiere = e, actives[j + 1]
    if frontiere is None or saut < 0.45:
        return None, float(saut)
    return frontiere, float(vals.max() - vals.min())

def png(nom, Y, U, V):
    try:
        from PIL import Image
        y = Y.astype(np.float32)
        u = np.repeat(U, 2, axis=1)[:, :y.shape[1]].astype(np.float32) - 128
        v = np.repeat(V, 2, axis=1)[:, :y.shape[1]].astype(np.float32) - 128
        r = y + 1.402 * v; g = y - 0.344136 * u - 0.714136 * v; b = y + 1.772 * u
        Image.fromarray(np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)).save(nom)
    except Exception as e:
        dire("  (PNG impossible : %s)" % e)

# ── Mode CALIBRAGE ───────────────────────────────────────────────────────────────────────────
# `--calibrer <flux>` : au lieu de guetter, imprime les valeurs de `r` par bande sur la première
# trame suspecte. C'est ainsi qu'on RE-RÈGLE un seuil au lieu de le deviner — mesuré le
# 2026-08-14 sur `mire_couleur.py --dechire` (déchirure CONNUE), une vraie déchirure donne
# min=0,50 max=1,00, soit un écart de 0,50 : juste au seuil. Un durcissement « raisonnable »
# (exiger min<0,20) supprimait la détection des vraies déchirures — d'où ce mode.
if "--calibrer" in sys.argv:
    _f = FLUX[0]
    _rd = bobimxl.Reader(bobimxl.Instance(), _f)
    _fm = _rd.format(); _w, _h = _fm["width"], _fm["height"]; _yb = _w * _h
    _buf, _vus, _n = [], set(), 0
    while _n < 600:
        _g = _rd.get_latest()
        if _g is None or _g[0] in _vus:
            continue
        _vus.add(_g[0]); _n += 1
        _buf.append(np.array(_g[2][:_yb].view(np.uint8).reshape(_h, _w)[:, ::2]).astype(np.float32))
        if len(_buf) < 3:
            continue
        _buf = _buf[-3:]
        _fr, _ec = dechirure(_buf[0], _buf[1], _buf[2])
        _hh = _buf[1].shape[0] // BANDES
        _r = []
        for _i in range(BANDES):
            _a = _buf[1][_i * _hh:(_i + 1) * _hh]
            _dp = float(np.abs(_a - _buf[0][_i * _hh:(_i + 1) * _hh]).mean())
            _ds = float(np.abs(_a - _buf[2][_i * _hh:(_i + 1) * _hh]).mean())
            _r.append(None if max(_dp, _ds) < 2.0 else _ds / (_dp + _ds + 1e-9))
        _act = [x for x in _r if x is not None]
        if len(_act) >= 4 and (max(_act) - min(_act)) > 0.4:
            print("  min=%.2f max=%.2f ecart=%.2f actives=%d  verdict=%s"
                  % (min(_act), max(_act), max(_act) - min(_act), len(_act),
                     ("DECHIRURE bande %d" % _fr) if _fr is not None else "sous le seuil"))
            print("  r par bande :", " ".join("%.2f" % x for x in _act))
            break
    sys.exit(0)

inst = bobimxl.Instance()
etat = {}
for n in FLUX:
    # `tampon` = les 3 derniers grains (N-1, N, N+1) : la dechirure s'evalue sur celui du MILIEU,
    # puisqu'il faut la trame SUIVANTE pour dire qu'une bande a pris de l'avance.
    etat[n] = {"rd": None, "tampon": [], "fi": None, "n": 0, "png": 0, "evt": 0}

dire("=== veille demarree sur %s (%.0f s) ===" % (", ".join(FLUX), DUREE))
t0 = time.time()
while time.time() - t0 < DUREE:
    for nom in FLUX:
        st = etat[nom]
        try:
            if st["rd"] is None:
                st["rd"] = bobimxl.Reader(inst, nom)
                fmt = st["rd"].format()
                st["w"], st["h"] = fmt["width"], fmt["height"]
                dire("%s : ouvert (%dx%d)" % (nom, st["w"], st["h"]))
            g = st["rd"].get_latest()
            if g is None or g[0] == st["fi"]:
                continue
            # ⚠⚠ N'EXAMINER QUE DES GRAINS COMPLETS. `get_latest()` peut rendre le grain EN COURS
            # d'ecriture (sa docstring le dit) : sur un flux TRANCHE il est alors partiellement
            # valide, et les bandes pas encore ecrites portent la trame precedente. Un lecteur
            # qui ignore validSlices FABRIQUE donc la « dechirure » qu'il croit observer — c'est
            # ce que faisaient les mesures du 2026-08-14 (18/min a lot=2, 5/min a lot=30 : on
            # mesurait la FREQUENCE DES COMMITS, pas un defaut de la chaine).
            if g[1].validSlices != g[1].totalSlices:
                st["partiels"] = st.get("partiels", 0) + 1
                continue
            st["fi"] = g[0]; st["n"] += 1
            w, h = st["w"], st["h"]
            yb, ub = w * h, (w // 2) * h
            v = g[2]
            Y = np.array(v[:yb].view(np.uint8).reshape(h, w)[:, ::2])       # grille chroma
            U = np.array(v[yb:yb + ub].view(np.uint8).reshape(h, w // 2))
            V = np.array(v[yb + ub:yb + 2 * ub].view(np.uint8).reshape(h, w // 2))
            st["tampon"].append((g[0], Y, U, V))
            if len(st["tampon"]) < 3:
                continue
            st["tampon"] = st["tampon"][-3:]
            (i0, Y0, _, _), (i1, Y1, U1, V1), (i2, Y2, _, _) = st["tampon"]
            fautes = []
            # Un decalage luma/chroma REEL est une propriete de la chaine : il est CONSTANT.
            # Le bruit, lui, erre d'une trame a l'autre. On n'annonce donc qu'une valeur STABLE
            # sur plusieurs trames consecutives, et une seule fois par episode.
            lag, force = decalage(Y1, U1, V1)
            hist = st.setdefault("lags", [])
            if lag is not None and force > 0.55:
                hist.append(lag)
                del hist[:-6]
                if len(hist) == 6 and len(set(hist)) == 1 and hist[0] != 0:
                    if st.get("lag_annonce") != hist[0]:
                        st["lag_annonce"] = hist[0]
                        fautes.append("DECALAGE luma/chroma STABLE %+d col sur 6 trames (corr %.2f)"
                                      % (hist[0], force))
            else:
                hist.clear(); st["lag_annonce"] = None
            fr, ecart = dechirure(Y0, Y1, Y2)
            if fr is not None:
                fautes.append("DECHIRURE frontiere bande %d/%d (ecart %.2f)" % (fr, BANDES, ecart))
            if fautes:
                st["evt"] += 1
                dire("%-18s grain %d : %s" % (nom, i1, " | ".join(fautes)))
                if st["png"] < MAX_PNG:
                    st["png"] += 1
                    png(os.path.join(SORTIE, "%s_%d.png" % (nom.replace("/", "_"), i1)), Y1, U1, V1)
        except Exception as e:
            dire("%s : lecture perdue (%s) — reouverture" % (nom, type(e).__name__))
            try: st["rd"].close()
            except Exception: pass
            st["rd"] = None; st["tampon"] = []
            time.sleep(1.0)
    time.sleep(0.004)

dire("=== veille terminee ===")
for n in FLUX:
    dire("  %-18s %d trames COMPLETES examinees, %d evenement(s), %d grains partiels ignores"
         % (n, etat[n]["n"], etat[n]["evt"], etat[n].get("partiels", 0)))
