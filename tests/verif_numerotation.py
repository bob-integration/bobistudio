#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Garde-fou de la convention « le 0 n'existe pas » — cf. `app/numerotation.py`, qui fait foi.

Une convention de nommage ne tient pas parce qu'elle est documentée : elle tient parce qu'un
test refuse le code qui la contourne. Sans ce fichier, le prochain `f"input_{i}"` écrit de bonne
foi rebranche silencieusement un conteneur sur une clé qui n'existe plus — et ça ne se voit
qu'au moment où un opérateur constate qu'une entrée n'est plus câblée.

    ./venv/bin/python tools/verif_numerotation.py

Sortie 0 = conforme. Sortie 1 = au moins une construction manuelle de clé/nom indexé.
"""

import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fichiers qui ont le DROIT de construire ces chaînes à la main : la source de vérité, son
# miroir dans l'image du moteur, la migration (qui manipule les DEUX formes par nature), et
# ce garde-fou.
EXEMPTS = {
    "app/numerotation.py",
    "app/migration_numerotation.py",
    "plugins/2110_io/docker/controller.py",   # miroir assumé (ne peut pas importer `app`)
    "tools/verif_numerotation.py",
}

# Constructions interdites hors des fichiers exempts.
INTERDITS = [
    (re.compile(r'f"input_\{[A-Za-z_]'),        'f"input_{…}" — utiliser cle_input()'),
    (re.compile(r'"input_%d"'),                 '"input_%d" — utiliser cle_input()'),
    (re.compile(r'"input_v_%d"'),               '"input_v_%d" — utiliser cle_input_v()'),
    (re.compile(r'"input_a_%d"'),               '"input_a_%d" — utiliser cle_input_a()'),
    (re.compile(r'f"tx\{[A-Za-z_][A-Za-z0-9_]*\}_shm"'), 'f"tx{…}_shm" — utiliser cle_tx_shm()'),
    (re.compile(r'"tx%d_shm"'),                 '"tx%d_shm" — utiliser cle_tx_shm()'),
    (re.compile(r'"tx_audio%d_shm"'),           '"tx_audio%d_shm" — utiliser cle_tx_audio_shm()'),
    (re.compile(r'"tx_anc%d_shm"'),             '"tx_anc%d_shm" — utiliser cle_tx_anc_shm()'),
    (re.compile(r'f"tx\{[A-Za-z_][A-Za-z0-9_]*\}:'), 'f"tx{…}:…" — utiliser slot_tx()'),
    (re.compile(r'f"[vad]:\{[A-Za-z_]'),        'f"v:{…}" — utiliser slot_rx()'),
    # ── NOMS DE FLUX MXL (ajouté le 2026-08-19) ──────────────────────────────────────────────
    # ANGLE MORT COMBLÉ : ce garde-fou ne surveillait que les CLÉS indexées. Les NOMS DE FLUX,
    # eux, se construisaient encore à la main un peu partout — et sont donc restés 0-based après
    # la migration du 2026-08-13, en silence. Ce que ça avait déjà cassé, sans une seule erreur :
    #   • `metrics.py` : fps par flux et badge « abonné mais ne reçoit pas » relus avec le VRAI
    #     nom (1-based) mais rangés sous un nom 0-based → `.get()` ne trouvait plus jamais rien ;
    #   • `plugins/2110_io/hooks.py` : le nom PROPOSÉ AU CÂBLAGE — donc du 0-based qui repartait
    #     s'écrire dans la config des consommateurs bien après la migration des données.
    # Le nom d'un flux dérive son UUID (uuid5) : une lettre de travers et le flux est un AUTRE.
    (re.compile(r'\+\s*\(?\s*"_%d"\s*%'),
     'nom de flux concaténé à la main — utiliser flux_video()/flux_audio()/flux_anc()'),
    (re.compile(r'"\{\}_\{\}"\.format\(\s*(?:hn|hostname|HOSTNAME)\b'),
     '"{}_{}".format(hostname, idx) — utiliser flux_video()'),
    (re.compile(r'f"\{(?:hn|hostname|HOSTNAME)\}_(?:audio_|anc_)?\{'),
     'f"{hostname}_{idx}" — utiliser flux_video()/flux_audio()/flux_anc()'),
]

# Les manifestes ne doivent plus indexer un nom de flux ni un state_field en 0-based.
INTERDITS_MANIFESTE = [
    (re.compile(r'"shm"\s*:\s*"[^"]*\{i\}'),          '"shm" avec {i} — utiliser {i1}'),
    (re.compile(r'"state_field"\s*:\s*"[^"]*\{i\}'),  '"state_field" avec {i} — utiliser {i1}'),
]


# Les `plugins/<type>/script.py` tournent DANS le conteneur : ils ne peuvent pas importer `app`,
# donc pas d'helper. Ils doivent malgré tout être 1-based — on vérifie que tout indice mis en clé
# y est explicitement décalé (`% (i + 1)`), et on refuse la forme nue (`% i`).
# ⚠ Ne PAS écrire ça avec un lookahead négatif après `\s*` : `\s*` peut revenir en arrière sur
# zéro caractère, ce qui rend la garde toujours satisfaite et le test toujours vert. On exige
# donc la forme POSITIVE de la faute — un identifiant collé au `%`, sans parenthèse de décalage.
_SCRIPT_CLE_NUE = re.compile(r'"(?:input(?:_[va])?|audio_shm)_%d"\s*%\s*[A-Za-z_]')
_SCRIPT_CLE_LITTERALE = re.compile(r'"(?:input(?:_[va])?|audio_shm)_0"')


def _fichiers():
    for base, dirs, noms in os.walk(RACINE):
        dirs[:] = [d for d in dirs
                   if d not in ("venv", ".git", "__pycache__", "old", "versions",
                                "node_modules", ".claude")]
        for n in noms:
            if n.endswith(".py") or n == "plugin.json":
                chemin = os.path.join(base, n)
                # Le vérificateur écrit EN TOUTES LETTRES les motifs qu'il traque : se scanner
                # lui-même produit un rapport où chaque règle se dénonce. Il vivait dans tools/,
                # hors du champ du walk par hasard ; le rangement du 2026-09-01 l'a mis dans
                # tests/, et il s'est mis à échouer sur son propre code — un faux positif qui
                # ne désigne aucun défaut, donc exactement ce qui fait qu'on cesse de lire un test.
                if os.path.abspath(chemin) == os.path.abspath(__file__):
                    continue
                yield chemin, os.path.relpath(chemin, RACINE)


def main():
    fautes = []
    for chemin, rel in _fichiers():
        if rel in EXEMPTS:
            continue
        try:
            txt = open(chemin, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        est_script = rel.startswith("plugins/") and rel.endswith("script.py")
        if rel.endswith("plugin.json"):
            regles = INTERDITS_MANIFESTE
        elif est_script:
            regles = [(_SCRIPT_CLE_NUE, "clé indexée non décalée — écrire % (i + 1)"),
                      (_SCRIPT_CLE_LITTERALE, "clé littérale en 0 — commencer à 1")]
        else:
            regles = INTERDITS
        for i, ligne in enumerate(txt.splitlines(), 1):
            nu = ligne.strip()
            if nu.startswith("#") or nu.startswith("//"):
                continue          # un commentaire a le droit de CITER la forme interdite
            for rx, motif in regles:
                if rx.search(ligne):
                    fautes.append("%s:%d — %s" % (rel, i, motif))
    if fautes:
        print("Convention de numérotation VIOLÉE (%d) :\n" % len(fautes))
        for f in fautes:
            print("  " + f)
        print("\nRègle et helpers : app/numerotation.py")
        return 1
    print("Numérotation conforme : aucune construction manuelle de clé ou de nom indexé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
