#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'ADRESSAGE du tally : par SOURCE, jamais par index de protocole.
#
# Ce que ce banc protège tient en une phrase : l'état de tally est indexé par la référence du
# signal, et l'index TSL n'existe plus qu'à la frontière du fil, dans l'adaptateur TSL. Deux
# pannes de production ont motivé cette bascule, et elles sont vérifiées ici toutes les deux :
#
#   1. 2026-09-01, PiP4. Une tuile basculée sur le mélangeur a gardé le rouge et le libellé
#      « HyperDeck 2 Out » — la source précédente. Cause : le distributeur traduisait la source
#      en index TSL, ne trouvait rien, et SAUTAIT la tuile ; le conteneur, qui n'applique que ce
#      qu'on lui pousse, restait sur son dernier état. Ne rien dire, ce n'est pas dire « éteint ».
#
#   2. Le même jour, IS-07 entrant refusait tout signal sans index TSL, et la propagation
#      abandonnait ses amonts pour la même raison. Un protocole absent du chemin décidait du
#      tally.
#
#   $ ./venv/bin/python tests/verif_tally_adressage.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from app import tally                                               # noqa: E402
from app import database as db                                      # noqa: E402

print("Tally — adressage par source\n")

NA, NB = "niv-a", "niv-b"


def _vider():
    with tally._lock:
        tally._tally_state.clear()
        tally._tally_par_source.clear()


# ═══ 1. LE REPLI DE COLONNE ════════════════════════════
#
# ★ REMONTER LES COLONNES, JAMAIS GARDER LE PRÉCÉDENT. Une colonne vide dit « ce flux n'a pas de
# libellé ici », pas « laisse en place celui d'avant ».
_labels = {}
db.db_get_source_label_for_shm = lambda shm, col: _labels.get((shm, int(col)), "")

_labels.clear()
_labels[("cam1", 4)] = "Caméra 4"
controle("★ la colonne demandée est servie quand elle est remplie",
         tally.libelle_de("cam1", 4) == "Caméra 4")

_labels.clear()
_labels[("cam1", 3)] = "Caméra plateau"
controle("★★★ colonne demandée VIDE → on remonte les colonnes",
         tally.libelle_de("cam1", 7) == "Caméra plateau",
         "demandé la 7, seule la 3 porte un texte. Obtenu %r" % tally.libelle_de("cam1", 7))

_labels.clear()
_labels[("cam1", 2)] = "deux"
_labels[("cam1", 5)] = "cinq"
controle("★★ l'ordre de repli est déclaré, et il commence aux colonnes SAISIES",
         tally.libelle_de("cam1", 9) == "deux" and tally.ORDRE_REPLI_COLONNES[0] == 2,
         "2→9 d'abord (des noms écrits par un humain), puis le nom d'hôte, puis le shm brut — "
         "qui est un identifiant, pas un libellé. Obtenu %r" % tally.libelle_de("cam1", 9))

_labels.clear()
controle("★★★ AUCUNE colonne remplie → chaîne vide, jamais le libellé d'un autre flux",
         tally.libelle_de("cam1", 4) == "",
         "★ C'EST LA PANNE PiP4. Un \"\" poussé jusqu'au conteneur EFFACE le texte ; ne rien "
         "renvoyer du tout le laisserait sur la source d'avant. Obtenu %r"
         % tally.libelle_de("cam1", 4))

_labels.clear()
_labels[("cam1", 2)] = "par la colonne 2"
controle("★★ `replier=False` s'en tient à la colonne demandée",
         tally.libelle_de("cam1", 6, replier=False) == "",
         "un UMD physique est câblé sur UNE colonne : lui envoyer le contenu d'une autre parce "
         "que la sienne est vide serait mentir sur ce qu'affiche l'écran")

# La ligne de libellé peut exister sous la forme brute OU sous la forme résolue selon l'époque.
_labels.clear()
_labels[("cam1", 2)] = "résolu"
tally._ports_cache["by_id"] = {42: {"kind": "source", "binding": {"shm": "cam1"}}}
tally._ports_cache["ts"] = 1e18       # gèle le cache : pas de relecture en base
controle("★★ un `port:<id>` trouve le libellé de la source à laquelle il est lié",
         tally.libelle_de("port:42", 2) == "résolu",
         "obtenu %r" % tally.libelle_de("port:42", 2))
tally._ports_cache["ts"] = 0

# ═══ 2. L'ADAPTATEUR TSL : L'INDEX S'ARRÊTE LÀ ══════════════════
from services import tsl                                            # noqa: E402

_mapping = []
db.db_get_tsl_mapping = lambda cid: list(_mapping)

srv = tsl._TslServer.__new__(tsl._TslServer)
import threading                                                    # noqa: E402
srv.conn_id, srv._lock = 77, threading.Lock()
srv._brut, srv._map_cache, srv._map_exp = {}, None, 0.0


def _repose(brut, mapping, depuis_zero=True):
    """Rejoue une réception TSL avec ce mapping, et rend l'état résultant.

    ⚠ `depuis_zero=False` EST LE CAS QUI COMPTE, et l'oublier a rendu ce banc muet à sa
    première écriture : en vidant l'état avant chaque pose, une contribution posée case-par-case
    devient indiscernable d'une contribution remplacée en entier. Or c'est justement la
    SURVIVANCE de l'ancienne case qui est la panne — mutation vérifiée."""
    global _mapping
    _mapping = mapping
    if depuis_zero:
        _vider()
    with srv._lock:
        srv._brut = dict(brut)
        srv._map_cache = None
    srv._republier()
    return dict(tally._tally_state)


MAP1 = [{"tsl_index": 3, "source_shm": "cam1"}, {"tsl_index": 4, "source_shm": "cam2"}]

etat = _repose({3: {NA: "red"}}, MAP1)
controle("★★★ l'index de TRAME est traduit en SOURCE avant d'entrer dans le modèle",
         etat == {("cam1", NA): "red"},
         "le modèle ne doit jamais voir le numéro 3 : il n'a de sens que sur le fil de cette "
         "connexion. Obtenu %s" % etat)

# ★ SANS VIDER : cam1 est déjà au rouge quand le mapping bascule sur cam2.
etat = _repose({3: {NA: "red"}}, [{"tsl_index": 3, "source_shm": "cam2"}], depuis_zero=False)
controle("★★★ le mapping change → le tally SUIT, et l'ancienne source s'éteint",
         etat == {("cam2", NA): "red"},
         "★ C'EST LA PANNE PiP4, prise à la source. La contribution du serveur est reposée en "
         "ENTIER : ce qu'il n'affirme plus disparaît. Une pose case-par-case aurait laissé cam1 "
         "au rouge pour toujours — un serveur TSL n'émet que sur changement, rien ne serait venu "
         "le corriger. Obtenu %s" % etat)

# Là encore sans vider : l'index 3 vient de perdre sa raison d'être, cam2 doit s'éteindre.
etat = _repose({9: {NA: "red"}}, MAP1, depuis_zero=False)
controle("★★★ un index SANS correspondance n'allume rien",
         etat == {},
         "l'index 9 ne désigne aucune source : l'allumer voudrait dire choisir un signal au "
         "hasard. Obtenu %s" % etat)

etat = _repose({3: {NA: "red"}, 4: {NA: "green"}},
               [{"tsl_index": 3, "source_shm": "cam1"}, {"tsl_index": 4, "source_shm": "cam1"}])
controle("★★ deux index de trame sur la MÊME source se cumulent",
         etat == {("cam1", NA): "amber"},
         "écraser ferait gagner le dernier index lu, arbitrairement. Obtenu %s" % etat)

etat = _repose({3: {NA: "red"}, 4: {NA: "red"}}, MAP1)
etat2 = _repose({3: {NA: "off"}, 4: {NA: "red"}}, MAP1, depuis_zero=False)
controle("★★ éteindre un index n'éteint pas les autres",
         ("cam1", NA) not in etat2 and etat2.get(("cam2", NA)) == "red",
         "obtenu %s" % etat2)

# ═══ 3. LE MODÈLE N'A PLUS DE PORTE POUR UN PROTOCOLE ══════════════
import inspect                                                      # noqa: E402

_sig_prop = list(inspect.signature(tally.propager).parameters)
controle("★★★ `propager` n'accepte plus de traducteur d'index",
         _sig_prop == ["etat", "par_shm", "etat_ctrl_de"],
         "tant qu'il en acceptait un, il fallait bien lui en fournir un — donc connaître un "
         "index quelque part. Obtenu %s" % _sig_prop)

_sig_ant = list(inspect.signature(tally._sortie_a_l_antenne).parameters)
controle("★★ ...ni `_sortie_a_l_antenne`",
         _sig_ant == ["ct", "niveaux"], "obtenu %s" % _sig_ant)

# Le distributeur lit l'état DIRECTEMENT. `index_chez` subsiste pour les protocoles SORTANTS ;
# ce qu'on interdit, c'est qu'il serve à LIRE.
_src_distrib = inspect.getsource(tally._distributor)
# Le CODE seul : les commentaires citent `index_chez` à dessein, pour dire ce qui a disparu.
_code_distrib = "\n".join(l for l in _src_distrib.splitlines()
                          if not l.lstrip().startswith("#"))
controle("★★★ le distributeur ne traduit plus rien pour lire l'état",
         "index_chez" not in _code_distrib,
         "c'est cette traduction qui faisait sauter la tuile PiP4. Trouvée dans : %s"
         % [l.strip() for l in _code_distrib.splitlines() if "index_chez" in l][:3])

controle("★★ le repli de colonne est bien celui du distributeur",
         "libelle_de" in _src_distrib,
         "le distributeur doit passer par le helper, pas relire une colonne unique")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
