#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""L'alerte de profondeur MXL doit nommer la BONNE panne.

Un domaine secondaire lève l'alerte même quand la profondeur est juste (`casse = dérive OR
autres`). Elle partait alors sur un gabarit de DÉRIVE, qui affiche « 200 ms, voulu 200 ms » —
contradiction apparente qui envoie chercher une dérive inexistante. On vérifie donc la clé
choisie ET le texte rendu, dans les deux langues."""
import sys; sys.path.insert(0, '/opt/bobistudio')
from app import node_health as nh
from app import i18n

alertes = []
nh.db_add_alert = lambda cle, niv="info", **k: alertes.append((cle, niv, k.get("params") or {}))
nh._cfg = lambda k, d: 400 if k == "mxl_history_ms" else d

NODE = {"id": 34, "name": "dl360-1"}

def sonde(sortie):
    """Une passe COMPLÈTE : la 1re arme seulement (cf. _check_prep_drift), la 2e sonde."""
    nh._mxl_history_drift_at.clear(); nh._mxl_history_alert_state.clear(); alertes.clear()
    # `_check_mxl_history_drift` fait `from . import node_driver` DANS la fonction : c'est
    # l'attribut du module app.node_driver qu'il faut remplacer, pas celui de node_health.
    import app.node_driver as nd
    nd.host_exec = lambda node, cmd, timeout=None: (0, sortie, "")
    nh._check_mxl_history_drift(34, "dl360-1", NODE)   # 1re passe : arme seulement
    nh._mxl_history_drift_at[34] = 0                   # échoit tout de suite
    nh._check_mxl_history_drift(34, "dl360-1", NODE)
    return alertes[0] if alertes else None

def cas(nom, sortie, cle_attendue, interdit_dans_texte=()):
    a = sonde(sortie)
    got = a[0] if a else None
    ok = (got == cle_attendue)
    detail = ""
    if a:
        for lang in ("fr", "en"):
            txt = i18n.t(a[0], lang, **a[2])
            if a[0] == txt:
                ok = False; detail += " [%s: clé NON traduite]" % lang
            for interdit in interdit_dans_texte:
                if interdit in txt:
                    ok = False; detail += " [%s: contient %r]" % (lang, interdit)
    print('%s %-46s clé=%s%s' % ('OK  ' if ok else '✕ KO', nom, got, detail))
    return ok

tout = True
# Référence : dérive seule, aucun domaine secondaire
tout &= cas("dérive seule", "200000000 persistant 0",
            "alert.prep.mxl_profondeur_derive_connue_unitok_seul")
# Le cas corrigé : profondeur JUSTE (400 ms) + 2 domaines secondaires
tout &= cas("conforme + 2 domaines secondaires", "400000000 persistant 2",
            "alert.prep.mxl_profondeur_conforme_unitok_autres",
            interdit_dans_texte=("voulu 400 ms", "target setting 400 ms"))
tout &= cas("conforme + secondaires + non persistant", "400000000 volatil 3",
            "alert.prep.mxl_profondeur_conforme_unitko_autres",
            interdit_dans_texte=("voulu 400 ms", "target setting 400 ms"))
# Dérive ET domaines secondaires : la dérive reste le sujet principal
tout &= cas("dérive + domaines secondaires", "200000000 persistant 1",
            "alert.prep.mxl_profondeur_derive_connue_unitok_autres")
# Conforme et SEUL : aucune alerte du tout
tout &= cas("conforme et seul : silence", "400000000 persistant 0", None)
# Pas de domaine MXL : silence
tout &= cas("sans domaine : silence", "sansdomaine -", None)

print("\n%s" % ("TOUT PASSE" if tout else "ÉCHEC"))
sys.exit(0 if tout else 1)
