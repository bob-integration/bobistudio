# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Version de Bobi.Studio — la SOURCE UNIQUE, et le comparateur qui va avec.

★ POURQUOI UN NUMÉRO DANS LE CODE. Il n'y en avait aucun : le numéro n'existait qu'au moment de
publier (`publier.sh --version`, à défaut la date du jour). Trois conséquences, toutes constatées
le 2026-09-02 — un exploitant ne pouvait pas dire quelle version il faisait tourner, la page Aide
n'avait rien à afficher, et surtout AUCUN composant ne pouvait exiger une version minimale, faute
de quelque chose à comparer.

`app/` est dans `CORE_DIRS` du builder : ce module est donc embarqué dans tout paquet installé,
sans passer par la liste blanche `CORE_FILES` — le piège d'un fichier `VERSION` à la racine, qui
marche en dev et rend un 404 sur une instance installée.

⚠ CETTE CONSTANTE ET LE TAG PUBLIÉ DOIVENT S'ACCORDER. `tools/publier.sh` le vérifie et REFUSE
de publier sur un désaccord : deux sources recopiées à la main finissent toujours par diverger,
et c'est le genre d'écart que personne ne remarque avant d'en avoir besoin.
"""

VERSION = "0.9.7"


def analyser(v):
    """« 0.9.2 » → (0, 9, 2). None dès qu'un segment n'est pas numérique.

    ★ NONE PLUTÔT QU'UN ORDRE INVENTÉ. Sur « 0.24-fix » ou « main », on ne compare pas du tout :
    un refus fondé sur une comparaison bancale serait pire que l'absence de contrôle. C'est la
    règle déjà appliquée aux images de nœud (`docker_compute`), reprise ici pour que les deux
    contrôles se comportent pareil.
    """
    v = str(v or "").strip().lstrip("vV")
    if not v:
        return None
    parts = v.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def au_moins(courante, minimale):
    """`courante` satisfait-elle l'exigence `minimale` ?

    Renvoie True quand l'exigence est vide (rien n'est exigé) ET quand la comparaison est
    impossible — ne pas savoir comparer n'autorise pas à bloquer un déploiement. L'appelant qui
    veut le TRACER utilise `comparable()`.

    Les longueurs inégales sont comblées par des zéros : 0.9 vaut 0.9.0, sinon « 0.9 » serait
    jugé antérieur à « 0.9.0 » et un exploitant n'aurait aucun moyen de le comprendre.
    """
    if not str(minimale or "").strip():
        return True
    a, b = analyser(courante), analyser(minimale)
    if a is None or b is None:
        return True
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a >= b


def comparable(courante, minimale):
    """False quand l'un des deux numéros n'est pas analysable — pour le journaliser."""
    if not str(minimale or "").strip():
        return True
    return analyser(courante) is not None and analyser(minimale) is not None


def core_min_de(manifeste):
    """Version minimale de Bobi.Studio exigée par ce manifeste (plugin ou service), ou ""."""
    req = (manifeste or {}).get("requires") or {}
    return str(req.get("core_min") or "").strip()
