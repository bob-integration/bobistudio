#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des LIBELLÉS VIVANTS du multiview : qui reçoit le texte poussé, et ce qu'il reçoit.
#
# ★ CE BANC N'EXISTAIT PAS, ET C'EST POURQUOI LA RÉGRESSION EST PARTIE EN PRODUCTION. En
# resserrant le distributeur sur `label_source == "protocol"`, j'ai supprimé le texte de toutes
# les fenêtres dont le MODÈLE porte un umd sourcé `tsl` — la configuration réelle du site. Le
# texte survivait tant que le conteneur tournait ; le premier redémarrage a montré un mur SANS
# AUCUN LIBELLÉ. Aucun des 46 bancs ne couvrait cette décision.
#
# Le plugin, lui, avait déjà la bonne règle sur son chemin TSL DIRECT (`wants_tsl_text`). Les deux
# chemins doivent la partager : c'est ce que vérifie le contrôle central de ce banc.
#
#   $ ./venv/bin/python tests/verif_multiview_libelles_vivants.py
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


from app import tally                                                # noqa: E402
import app.database as db                                            # noqa: E402

print("Multiview — libellés vivants\n")

UMD_TSL  = {"type": "umd", "text_source": "tsl"}
UMD_NOM  = {"type": "umd", "text_source": "name"}
VIDEO    = {"type": "video"}

# ═══ 1. QUI VEUT LE TEXTE ════════════════════════════
controle("★★★ un umd sourcé `tsl` dans le MODÈLE DE LA FENÊTRE veut le texte",
         tally.veut_texte_pousse({"template": {"components": [VIDEO, UMD_TSL]}}, {}),
         "★ C'EST LA RÉGRESSION. Le distributeur ne regardait que `label_source`, et ces "
         "fenêtres-là — en `hostname`, avec un umd `tsl` — ne recevaient plus rien.")

controle("★★★ ...et dans le modèle PAR DÉFAUT DU MUR aussi",
         tally.veut_texte_pousse({}, {"default_template": {"components": [VIDEO, UMD_TSL]}}),
         "c'est la configuration du site : les fenêtres n'ont pas de modèle propre, elles "
         "héritent de celui du mur")

controle("★★ le libellé classique en mode `protocol` le veut toujours",
         tally.veut_texte_pousse({"show_label": True, "label_source": "protocol"}, {}))

controle("★★ ...mais PAS s'il est masqué",
         not tally.veut_texte_pousse({"show_label": False, "label_source": "protocol"}, {}),
         "`_is_protocol_label` exige les deux côté plugin : les deux chemins doivent s'accorder")

# Le filtre n'est pas cosmétique : un texte poussé à une fenêtre qui ne l'affiche pas la fait
# quand même re-baker son habillage plein cadre (~25 ms, une trame perdue).
controle("★★★ une fenêtre qui n'affiche AUCUN texte poussé n'en reçoit pas",
         not tally.veut_texte_pousse(
             {"show_label": True, "label_source": "hostname"},
             {"default_template": {"components": [VIDEO, UMD_NOM]}}),
         "un changement de glyphes n'est pas énumérable : le mur re-bake plein cadre. Pousser à "
         "qui ne lit pas coûte une trame pour rien")

# ★ Le modèle de la FENÊTRE prime, comme `_tpl_comps` : c'est l'héritage, pas une union.
controle("★★★ le modèle de la fenêtre PRIME sur celui du mur",
         not tally.veut_texte_pousse(
             {"template": {"components": [VIDEO, UMD_NOM]}},
             {"default_template": {"components": [VIDEO, UMD_TSL]}}),
         "une fenêtre qui a choisi son modèle ne doit pas hériter en plus de celui du mur — "
         "sinon l'héritage devient une union, et on ne peut plus RIEN désactiver localement")

controle("★ une fenêtre sans modèle ni libellé protocole ne veut rien",
         not tally.veut_texte_pousse({}, {}))

# ═══ 2. LES COLONNES POUSSÉES ════════════════════════
_T = [{"shm": "cam1", "label_2": "Caméra 1", "label_5": "CAM1", "projet": "Studio"}]
db.db_get_source_labels = lambda: list(_T)
tally.invalider_libelles()

cols, projet = tally.colonnes_de("cam1")
controle("★ les colonnes remplies sont poussées",
         cols.get("2") == "Caméra 1" and cols.get("5") == "CAM1" and projet == "Studio",
         "obtenu %s / %r" % (cols, projet))

controle("★★★ les HUIT colonnes sont poussées, vides comprises",
         sorted(cols) == [str(n) for n in range(2, 10)],
         "côté conteneur une colonne absente et une colonne vide ne se distinguent pas : ne "
         "pousser que les remplies laisserait les autres sur leur valeur d'avant — la faute même "
         "que ce canal existe pour supprimer. Obtenu %s" % sorted(cols))

cols, projet = tally.colonnes_de("flux_inconnu")
controle("★★★ un flux SANS ligne reçoit huit colonnes VIDES, pas rien",
         all(v == "" for v in cols.values()) and len(cols) == 8 and projet == "",
         "ne rien envoyer laisserait le libellé de la source précédente : c'est exactement la "
         "panne PiP3. Obtenu %s" % cols)

# ★ PAS de repli de colonne ici, à la différence de `libelle_de`.
cols, _ = tally.colonnes_de("cam1")
controle("★★ aucun repli de colonne : `%%src_label3%%` désigne LA colonne 3".replace("%%", "%"),
         cols.get("3") == "",
         "un gabarit qui écrit `%%src_label3%%` demande la 3 ; lui servir la 2 afficherait autre "
         "chose que ce qu'il demande. Le repli du conteneur — le nom de la fenêtre — est le bon. "
         "Obtenu %r" % cols.get("3"))

# ═══ 3. L'INSTANTANÉ NE DOIT PAS FIGER ═══════════════
_T[:] = [{"shm": "cam1", "label_2": "Caméra UN", "projet": ""}]
tally.invalider_libelles()
cols, _ = tally.colonnes_de("cam1")
controle("★★★ une écriture invalide l'instantané SANS attendre son échéance",
         cols.get("2") == "Caméra UN",
         "sans ça l'exploitant voit son texte refusé une seconde durant et se demande s'il a été "
         "pris en compte. Obtenu %r" % cols.get("2"))

controle("★★ ...et l'échéance reste un filet, courte",
         0 < tally._LABELS_TTL_S <= 2.0,
         "l'invalidation explicite peut être oubliée sur un futur site d'écriture ; l'échéance "
         "garantit qu'un oubli coûte une seconde, jamais un libellé figé pour toujours. "
         "Obtenu %s s" % tally._LABELS_TTL_S)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
