# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Lecture du réglage `video_formats` — pendant PYTHON de `static/video_formats.js`.

Le réglage est la SOURCE UNIQUE des formats vidéo du site (Réglages → Vidéo). Une ligne =
``label ; width ; height ; fps ; scan ; chroma ; bit_depth ; colorimetry``
(ex. ``3G 1080p50 ;1920;1080;50;p;422;10;709``).

Côté navigateur, `static/video_formats.js` porte déjà l'avertissement : ce parseur avait été
recopié de page en page, et chaque copie était une seconde vérité qui dérivait en silence. Le
même piège existait côté Python — `app/clocks.py` découpe la ligne à la main pour en tirer la
cadence de référence, et rien ne garantissait que les deux lectures s'accordent. Ce module est
la lecture Python unique ; il applique EXACTEMENT les mêmes valeurs de repli que le JS (chroma
422, profondeur 10, scan progressif, colorimétrie 709) pour qu'un même réglage donne le même
format des deux côtés.

⚠ **Convention `fps` : l'entrelacé compte les CHAMPS.** « HD 1080i50 » porte `fps=50` et
`scan='i'` — c'est-à-dire 50 champs, soit 25 trames par seconde. Tout consommateur qui raisonne
en TRAMES (le `grain_rate` NMOS, par exemple) doit diviser par deux : `frame_rate()` le fait.
"""

import re

CHROMAS = ("420", "422", "444")
BIT_DEPTHS = (8, 10, 12)


def _parse_ligne(ligne):
    """Une ligne de réglage → dict, ou None si elle n'est pas exploitable.

    Mêmes replis que `static/video_formats.js` : une valeur hors domaine n'invalide PAS la ligne,
    elle retombe sur le défaut (c'est le comportement que l'UI donne déjà à l'exploitant). Seuls
    un label vide ou une géométrie nulle écartent la ligne."""
    champs = [c.strip() for c in (ligne or "").split(";")]

    def _champ(i):
        return champs[i] if i < len(champs) else ""

    try:
        w = int(_champ(1) or 0)
        h = int(_champ(2) or 0)
    except ValueError:
        return None
    try:
        fps = float(_champ(3) or 0) or 25.0
    except ValueError:
        fps = 25.0
    try:
        bd = int(_champ(6))
    except (TypeError, ValueError):
        bd = 10
    label = _champ(0)
    if not label or not w or not h:
        return None
    return {
        "label":       label,
        "w":           w,
        "h":           h,
        "fps":         fps,
        "scan":        "i" if _champ(4).lower() == "i" else "p",
        "chroma":      _champ(5) if _champ(5) in CHROMAS else "422",
        "bit_depth":   bd if bd in BIT_DEPTHS else 10,
        "colorimetry": (_champ(7) or "709").lower(),
    }


def formats(reglage=None):
    """Liste des formats vidéo déclarés au site, dans l'ordre du réglage.

    `reglage` : contenu brut (sinon lu dans les settings). Renvoie [] si rien n'est déclaré —
    un site sans formats n'est pas une erreur, c'est une installation qui n'a pas encore été
    configurée, et l'appelant doit pouvoir le dire plutôt que de fabriquer un format par défaut."""
    if reglage is None:
        from . import settings as _s
        reglage = _s.get("video_formats") or ""
    out = []
    for ligne in str(reglage).splitlines():
        if not ligne.strip():
            continue
        f = _parse_ligne(ligne)
        if f:
            out.append(f)
    return out


def frame_rate(fmt):
    """Cadence TRAME (et non champ) d'un format, en (numérateur, dénominateur) EXACTS.

    Deux corrections que personne ne doit refaire dans son coin :

    1. **Entrelacé** : `fps` compte les CHAMPS dans le réglage (1080i50 → 50), or une trame vaut
       deux champs — on divise donc par deux. Une cadence NMOS `grain_rate` est une cadence de
       trames, pas de champs.
    2. **Cadences NTSC** : 29,97 n'est pas un nombre décimal, c'est 30000/1001. Les écrire en
       flottant produirait un `grain_rate` que le contrôleur d'en face ne reconnaîtrait pas
       comme égal au sien. On rétablit donc la fraction exacte quand la valeur est à moins de
       0,01 d'un multiple de 1000/1001."""
    fps = float(fmt["fps"])
    if fmt.get("scan") == "i":
        fps /= 2.0
    entier = round(fps)
    # Cadence « /1.001 » (23,976 / 29,97 / 59,94…) : l'entier le plus proche divisé par 1,001.
    if entier and abs(fps - (entier * 1000.0 / 1001.0)) < 0.01:
        return entier * 1000, 1001
    if abs(fps - entier) < 0.001:
        return int(entier), 1
    # Cadence exotique : on la rend en millièmes plutôt que d'arrondir en silence.
    return int(round(fps * 1000)), 1000


def anomalie(fmt):
    """Message décrivant une INCOHÉRENCE STRUCTURELLE de la ligne de réglage, ou None.

    Un seul contrôle aujourd'hui, mais il ne repose sur AUCUN seuil arbitraire — c'est une
    propriété de l'entrelacement lui-même : une trame vaut exactement deux champs, donc un format
    entrelacé dont le nombre de CHAMPS par seconde est impair décrit une cadence de trames
    demi-entière, ce qui n'existe pas. Relevé sur ce site le 2026-08-15 : « SD-SDI PAL » porte
    ``fps=25 ; scan=i``, soit 12,5 trames/s — la ligne devrait porter 50 (le PAL, c'est 25 trames
    = 50 champs). La convention de la colonne `fps` est bien « champs si entrelacé » (cf.
    `io2110_layouts.py` et `static/tx_models.js`), donc c'est la ligne qui est fausse.

    On ne corrige PAS d'office (multiplier par deux serait deviner l'intention de l'exploitant, et
    la même ligne alimente le calcul de débit pixel et la cadence de référence d'horloge) : on
    rend l'anomalie visible et on laisse l'appelant décider s'il peut travailler avec."""
    fps = float(fmt.get("fps") or 0)
    label = str(fmt.get("label") or "")

    # Règle 1 — L'INTENTION DÉCLARÉE DANS LE LIBELLÉ. Nos libellés encodent le format
    # (« HD 1920×1080i50 », « 3G 1080p59.94 ») : le nombre qui suit le `i`/`p` EST la cadence
    # voulue par l'exploitant, dans la convention de la colonne (champs si entrelacé). On compare
    # donc la valeur à l'intention plutôt qu'à une liste de cadences « standard » qu'il faudrait
    # tenir à jour. C'est cette règle qui attrape « HD 1920×1080i59.94 » déclaré à 29,97.
    m = re.search(r"[ip](\d+(?:[.,]\d+)?)\s*$", label.strip(), re.IGNORECASE)
    if m:
        voulu = float(m.group(1).replace(",", "."))
        if abs(voulu - fps) > 0.01:
            return ("format « %s » : le libellé annonce %g, la colonne `fps` porte %g. Pour "
                    "l'entrelacé cette colonne compte les CHAMPS (1080i50 → 50) — la ligne devrait "
                    "vraisemblablement porter %g." % (label, voulu, fps, voulu))

    # Règle 2 — repli STRUCTUREL quand le libellé ne dit rien (« SD-SDI PAL »). Aucun seuil
    # arbitraire : une trame vaut exactement deux champs, donc un nombre IMPAIR de champs par
    # seconde décrit une cadence de trames demi-entière, qui n'existe pas.
    if fmt.get("scan") == "i" and abs(fps - round(fps)) < 0.001 and int(round(fps)) % 2:
        return ("format entrelacé « %s » à %g champs/s : nombre de champs IMPAIR, soit %g trames/s. "
                "La colonne `fps` compte les CHAMPS pour l'entrelacé (1080i50 → 50) — cette ligne "
                "devrait vraisemblablement porter %d." % (label, fps, fps / 2.0, int(round(fps)) * 2))
    return None


def interlace_mode(fmt):
    """Mode d'entrelacement NMOS d'un format.

    L'ordre des champs suit la règle DÉJÀ appliquée par `scripts.py` au rendu des scripts (HD/UHD
    ≥ 720 lignes = TFF, SD ≤ 576 = BFF) : on ne réinvente pas une seconde convention ici, sans
    quoi un même format serait annoncé TFF en NMOS et traité BFF par le producteur."""
    if fmt.get("scan") != "i":
        return "progressive"
    return "interlaced_bff" if int(fmt.get("h") or 0) <= 576 else "interlaced_tff"


def color_sampling(fmt):
    """Échantillonnage chroma au vocabulaire du registre NMOS (`YCbCr-4:2:2`…)."""
    return "YCbCr-4:%s:%s" % (fmt.get("chroma", "422")[1], fmt.get("chroma", "422")[2])


def colorspace(fmt):
    """Colorimétrie au vocabulaire du registre NMOS, ou None si le réglage porte une valeur
    qu'on ne sait pas traduire — auquel cas il vaut mieux NE RIEN annoncer que d'annoncer faux :
    une contrainte absente signifie « pas de contrainte », une contrainte fausse fait rejeter
    une source parfaitement valide."""
    return {"709": "BT709", "601": "BT601", "2020": "BT2020",
            "2100": "BT2100"}.get(str(fmt.get("colorimetry", "")).lower())
