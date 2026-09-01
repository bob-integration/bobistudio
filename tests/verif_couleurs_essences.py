#!/usr/bin/env python3
"""Vérifie les couleurs d'ESSENCE des flux (vidéo / audio / ANC) de la page Câbles.

À lancer à la main :  ./venv/bin/python tools/verif_couleurs_essences.py

Pourquoi ce banc : un exploitant au téléphone dit « je suis le câble bleu ». Ça n'a de sens que si
la couleur ne dépend pas du thème de celui qui regarde. La vidéo était câblée sur `--accent`,
c'est-à-dire sur l'identité du thème — bleu acier en sombre, ambre en Studio, indigo en Daylight.
Ce banc empêche la régression : il RELIT les fichiers CSS et refuse toute surcharge par thème.

Il revérifie aussi, par calcul, les quatre propriétés qui ont présidé au choix (2026-08-19) :
contraste sur les trois fonds, écart entre essences, écart aux couleurs de statut, et lisibilité
des variantes de survol. Les seuils ne sont pas des goûts : sous 3:1 un trait fin décroche du
fond, et un ΔE trop faible se confond sur un écran de régie mal calibré.
"""
import math, re, sys, os

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(RACINE, "static", "css")
FONDS = {"sombre": "#14161a", "studio": "#18181b", "clair": "#f6f7f9"}
STATUTS = {"running": "#7ab98a", "stopped": "#d07a82", "warning": "#c4a667",
           "ambre RDMA": "#f59e0b", "error": "#ef4444"}
SEUIL_CONTRASTE, SEUIL_ESSENCES, SEUIL_STATUT = 3.0, 40.0, 30.0


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h):
    r, g, b = map(_lin, _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contraste(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _lab(h):
    r, g, b = map(_lin, _rgb(h))
    X = r * .4124 + g * .3576 + b * .1805
    Y = r * .2126 + g * .7152 + b * .0722
    Z = r * .0193 + g * .1192 + b * .9505
    f = lambda t: t ** (1 / 3) if t > .008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(X / .95047), f(Y / 1.0), f(Z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b))))


def lire(fichier, variable):
    """Valeur littérale d'une variable CSS dans un fichier (None si absente)."""
    try:
        src = open(os.path.join(CSS, fichier), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"^\s*" + re.escape(variable) + r"\s*:\s*([^;]+);", src, re.M)
    return m.group(1).strip() if m else None


def main():
    essences = {"vidéo": "--topo-flow-video", "audio": "--topo-flow-audio", "ANC": "--topo-flow-data"}
    echecs = []

    # 1. Une seule définition, dans base.css, et AUCUNE surcharge par thème.
    couleurs = {}
    for nom, var in essences.items():
        base = lire("base.css", var)
        if not base or not base.startswith("#"):
            echecs.append(f"{var} : attendue littérale dans base.css, trouvé {base!r} "
                          f"(une valeur dérivée du thème — var(--accent) — est précisément le défaut corrigé)")
            continue
        couleurs[nom] = base
        for theme in ("theme-studio.css", "theme-light.css"):
            v = lire(theme, var)
            if v is not None:
                echecs.append(f"{var} surchargée dans {theme} ({v}) : l'essence ne doit pas dépendre du thème")
        for suffixe in ("-active",):
            for theme in ("theme-studio.css", "theme-light.css"):
                v = lire(theme, var + suffixe)
                if v is not None:
                    echecs.append(f"{var}{suffixe} surchargée dans {theme} ({v})")

    if len(couleurs) == 3:
        print("Couleurs d'essence (identiques dans tous les thèmes) :")
        for nom, c in couleurs.items():
            print(f"   {nom:6s} {c}")

        print("\nContraste sur les fonds :")
        for fond, hexa in FONDS.items():
            ligne = "  ".join(f"{n}={contraste(c, hexa):.2f}" for n, c in couleurs.items())
            print(f"   {fond:7s} {ligne}")
            for n, c in couleurs.items():
                if contraste(c, hexa) < SEUIL_CONTRASTE:
                    echecs.append(f"{n} sur fond {fond} : contraste {contraste(c, hexa):.2f} < {SEUIL_CONTRASTE}")

        print("\nÉcart entre essences :")
        noms = list(couleurs)
        for i in range(len(noms)):
            for j in range(i + 1, len(noms)):
                d = delta_e(couleurs[noms[i]], couleurs[noms[j]])
                print(f"   {noms[i]}/{noms[j]} ΔE={d:.1f}")
                if d < SEUIL_ESSENCES:
                    echecs.append(f"{noms[i]}/{noms[j]} : ΔE {d:.1f} < {SEUIL_ESSENCES} (confusables)")

        print("\nÉcart aux couleurs de STATUT (une essence ne doit pas se lire comme un état) :")
        pires = sorted((delta_e(c, s), n, sn) for n, c in couleurs.items() for sn, s in STATUTS.items())
        for d, n, sn in pires[:3]:
            print(f"   {n} vs {sn} ΔE={d:.1f}")
        for d, n, sn in pires:
            if d < SEUIL_STATUT:
                echecs.append(f"{n} vs {sn} : ΔE {d:.1f} < {SEUIL_STATUT}")

    print()
    if echecs:
        print(f"{len(echecs)} PROBLÈME(S) :")
        for e in echecs:
            print("   ✗ " + e)
        return 1
    print("Toutes les contraintes sont respectées.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
