#!/usr/bin/env python3
"""Décode les BANDEAUX-SONDE d'une trame RÉELLE, à toutes les échelles de proxy.

Le générateur (plugins/avsync) et la sonde (plugins/sonde_latence) sont deux scripts autonomes :
rien ne garantit qu'ils parlent le même format, sinon de le VÉRIFIER. Ce banc prend une image
produite par le générateur et la relit avec le décodeur de la sonde, à 1/1, 1/2, 1/4, 1/8 et
1/16 — les échelles auxquelles descendent les proxies de la pyramide.

Il verrouille en particulier le mode « bords » (cartouche C+) : nombre de blocs DÉRIVÉ de la
largeur source, donc inconnu du lecteur, qui doit le retrouver en essayant les formats candidats
et en laissant la somme de contrôle arbitrer. Un format faux a 1 chance sur 16 de passer par
hasard sur UNE bande ; il faudrait qu'il passe sur les DEUX, avec le même index et les numéros
0 et 15 attendus.

    ./venv/bin/python tools/verif_sonde_format.py <image.png>
"""
import os
import sys

import numpy as np
from PIL import Image

FORMATS = (32, 24, 20)
ZONE_X = (0.135, 0.985)          # zone des blocs, mode cartouche
FRAC_BORDS = (0.10, 0.90)
BANDE_BAS = 15
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def luma(img):
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    return 16.0 + (0.257 * a[..., 0] + 0.504 * a[..., 1] + 0.098 * a[..., 2])


def _extraire(nom, src):
    """Extrait une fonction du script de plugin, accolades du gabarit `str.format` dédoublées.

    Le décodeur était RECOPIÉ ici, et pouvait donc dériver du code réel sans que rien ne le
    signale — exactement ce que ce banc est censé empêcher ailleurs. On l'extrait, comme
    verif_render_node.js le fait pour renderNode : le banc ne peut plus valider un décodeur
    que le plugin n'utilise pas."""
    i = src.index("def %s(" % nom)
    lignes = src[i:].split("\n")
    out = [lignes[0]]
    for l in lignes[1:]:
        if l and not l[0].isspace():
            break
        out.append(l)
    return "\n".join(out).replace("{{", "{").replace("}}", "}")


_PLUGIN = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "plugins", "sonde_latence", "script.py"), encoding="utf8").read()
_ns = {"np": np}
exec(_extraire("_decode_ligne", _PLUGIN), _ns)
decode_ligne = _ns["_decode_ligne"]


def lire_bords(y):
    """Essaie les formats candidats sur les deux bandeaux ; rend (index, nb) ou (None, None)."""
    h = y.shape[0]
    for nb in FORMATS:
        res = []
        for frac, attendu in zip(FRAC_BORDS, (0, BANDE_BAS)):
            r = decode_ligne(y[min(h - 1, int(frac * h))], nb, *ZONE_X)
            if r is None or r[1] != attendu:
                res = None
                break
            res.append(r[0])
        if res and res[0] == res[1]:
            return res[0], nb
    return None, None


def run(path):
    src = Image.open(path)
    W, H = src.size
    zone = int((ZONE_X[1] - ZONE_X[0]) * W)
    attendu_nb = next((n for n in FORMATS if zone / n >= 60), FORMATS[-1])
    print("image %dx%d · zone de blocs %d px · format attendu %d blocs (%.0f px/bloc)\n"
          % (W, H, zone, attendu_nb, zone / attendu_nb))

    # ── L'image doit être EN DISPOSITION « BORDS » ───────────────────────────────────────
    # Ce banc ne vérifie que le cartouche. Une trame en disposition « tranche » est parfaitement
    # valide, mais ses bandeaux sont ailleurs et pleine largeur : la relire avec la géométrie du
    # cartouche donne cinq « ÉCHEC » qui n'accusent rien. Un banc qui crie sur une entrée saine
    # apprend à ignorer ses propres alarmes — on refuse l'entrée, on ne la note pas.
    if lire_bords(luma(src))[0] is None:
        print("Cette image ne porte pas de cartouche lisible à 1/1.\n"
              "Si elle est en disposition « tranche » (bandeaux pleine largeur), c'est NORMAL :\n"
              "ce banc ne vérifie que la disposition « bords ». Relancez la mire avec\n"
              "probe_layout=bords, recapturez, puis relancez ce banc.")
        return 2

    ref = None
    for div in (1, 2, 4, 8, 16):
        im = src if div == 1 else src.resize((max(8, W // div), max(8, H // div)), Image.BILINEAR)
        idx, nb = lire_bords(luma(im))
        lisible = idx is not None
        bloc_px = (zone / attendu_nb) / div
        titre = "1/%-2d  (%4dx%-4d · %.1f px/bloc)" % (div, im.size[0], im.size[1], bloc_px)
        if lisible and ref is None:
            ref = idx
        ok(lisible, titre + " → décodé", "illisible à cette échelle")
        if lisible:
            ok(nb == attendu_nb, titre + " → format retrouvé (%d blocs)" % attendu_nb,
               "format retenu %s au lieu de %s" % (nb, attendu_nb))
            ok(idx == ref, titre + " → MÊME index qu'à pleine résolution",
               "index %s ≠ %s" % (idx, ref))


    # ── L'ÂGE se calcule modulo la largeur d'index DÉTECTÉE, jamais 2^24 en dur ──────────────
    # Le mode cartouche code l'index sur 16 ou 12 bits selon la largeur source. Un modulo 2^24
    # figé rendrait un âge absurde dès qu'on quitte les 32 blocs — faux, et d'allure crédible.
    # Contrôle statique : c'est une régression facile à réintroduire d'un copier-coller.
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "plugins", "sonde_latence", "script.py"), encoding="utf8").read()
    ok("(1 << idx_bits)" in src, "l'âge utilise la largeur d'index DÉTECTÉE",
       "le calcul d'âge n'emploie pas `idx_bits`")
    ok("% (1 << 24)" not in src, "aucun modulo 2^24 codé en dur ne subsiste",
       "un `% (1 << 24)` figé demeure dans le script")

    # ── Panneau : `zip(bornes, vals)` TRONQUE EN SILENCE si les deux listes divergent ────────
    # Ajouter une colonne d'en-tête sans sa valeur (ou l'inverse) ne lève rien : la colonne
    # disparaît simplement du panneau. On compte donc les trois listes — en-têtes, largeurs,
    # valeurs — et on exige qu'elles s'accordent.
    import re as _re
    _i = src.index("        vals = [")
    _b = src[_i:src.index("\n        ]", _i)]
    _prof, _nv = 0, 0
    for _ch in _b[_b.index("["):]:
        if _ch == "(":
            if _prof == 1:
                _nv += 1
            _prof += 1
        elif _ch == ")":
            _prof -= 1
        elif _ch == "[":
            _prof += 1
        elif _ch == "]":
            _prof -= 1
    _nc = src.count("_c[", src.index("cols = ["), src.index("fh = _font"))
    _nh = [len(_re.findall(r'"[^"]*"', m)) for m in _re.findall(r'"col": \(([^)]*)\)', src)]
    ok(_nv == _nc and all(h == _nc for h in _nh),
       "panneau : %d valeurs = %d colonnes = en-têtes %s" % (_nv, _nc, _nh),
       "divergence — zip() tronquerait la colonne en trop, sans rien signaler")
    print("\n%s" % ("Format vérifié à toutes les échelles."
                    if not ko else "%d contrôle(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not os.path.exists(sys.argv[1]):
        print("usage : verif_sonde_format.py <image.png>"); sys.exit(2)
    sys.exit(run(sys.argv[1]))
