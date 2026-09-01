# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Bibliothèque des modèles de PiP (multiview) : modèles d'USINE (builtin, non modifiables,
servis avec la bibliothèque DB) + résolution nom → composants.

Un modèle = {"components": [composant…]} ; composant = {"id", "type", "x","y","w","h"
(normalisés 0..1 relatifs à la cellule), "when" (condition), "min_w" (repli petites tuiles),
+ réglages par type}. Types : video / umd / tally / meters / anc / clock / text / format /
video_history / audio_history.
Le rendu vit dans plugins/multiview/script.py (section « Modèles de PiP ») ; l'éditeur dans
Réglages → PiP (static/pip_editor.js). Les modèles affectés sont EMBARQUÉS (résolus) dans
deploy_config.flux_config[i].template → snapshotés/restaurés avec les projets.

Composant `meters` — `width_mode` (opt-in, DÉFAUT absent = "auto", libellé UI « Fixe ») :
"auto" = largeur intrinsèque fixe (`w` du composant sert seulement de zone d'ancrage pour
`align` left/center/right) ; "fit" (libellé UI « Suit la largeur du composant ») = le VU-mètre
REMPLIT `w` — mais SEULES les barres de canaux s'élargissent, la zone de graduations (dB/PPM,
largeur `METER_TICK_W`) et l'espacement inter-canaux (`METER_GAP`) restent FIXES (sinon
l'échelle et ses repères se déforment) ; `align` alors sans effet. Repli automatique sur la
largeur intrinsèque si `w` est trop étroit pour une barre lisible (≥ 2 px).
Voir `_meter_layout`/`_meter_fit_dims` dans plugins/multiview/script.py.

Composants `video_history` / `audio_history` (multiview 0.37.0) — frises de diagnostic « que
s'est-il passé sur cette source ? », posées dans une cellule (la SOURCE est celle de la fenêtre :
vidéo pour l'une, audio pour l'autre — rien à câbler dans le modèle) :
  video_history : {duration (10|30|60|120 s, défaut 30), opacity, events} — une vignette par
    SECONDE + ruban gel/noir/perte de signal ; la vignette de l'INSTANT de l'événement est
    épinglée dans sa case (liseré coloré) ;
  audio_history : {duration, opacity, channels, ch_start} — enveloppe des crêtes, saturation
    (colonne rouge persistante), silence (plage grisée).
Les MÊMES outils existent en BLOCS LIBRES DU MUR (hors de toute cellule) :
deploy_config.params.video_history_blocks[] / audio_history_blocks[] (mêmes clés + géométrie en
fractions du MUR + source câblée propre), cf. plugins/multiview/script.py render_history_tiles."""

BUILTIN_PIP_TEMPLATES = [
    {
        # Réplique STATIQUE du rendu historique par défaut (bandeau nom translucide en bas de
        # l'image + pavés tally G/D). Miroir du modèle GÉNÉRÉ côté moteur (_classic_comps,
        # plugins/multiview/script.py) — c'est aussi le repli d'une cellule sans modèle.
        "id": "builtin:classic",
        "name": "Classique (bandeau + tally)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
             "fit": "fill", "border": "none"},
            {"id": "umd", "type": "umd", "x": 0.0, "y": 0.92, "w": 1.0, "h": 0.08,
             "text_source": "name", "tally_bg": False, "bg_color": "#000000", "bg_opacity": 70},
            {"id": "talL", "type": "tally", "x": 0.01, "y": 0.93, "w": 0.032, "h": 0.056,
             "shape": "bar", "slot": "L", "min_w": 140},
            {"id": "talR", "type": "tally", "x": 0.958, "y": 0.93, "w": 0.032, "h": 0.056,
             "shape": "bar", "slot": "R", "min_w": 140},
        ]},
    },
    {
        # Ex-« texte sous l'image » (overlay_below) : PAS de mécanisme dédié, c'est un LAYOUT
        # de modèle — vidéo réduite + bandeau UMD broadcast SOUS l'image, cadre fin.
        "id": "builtin:umd-below",
        "name": "UMD broadcast (texte sous l'image)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.87,
             "fit": "fill", "border": "classic", "border_w": 2},
            {"id": "lampL", "type": "tally", "x": 0.03, "y": 0.895, "w": 0.05, "h": 0.085,
             "shape": "lamp", "slot": "L", "min_w": 160},
            {"id": "umd", "type": "umd", "x": 0.14, "y": 0.885, "w": 0.72, "h": 0.105,
             "text_source": "name", "tally_bg": False, "bg_color": "#08080a", "bg_opacity": 100},
            {"id": "lampR", "type": "tally", "x": 0.92, "y": 0.895, "w": 0.05, "h": 0.085,
             "shape": "lamp", "slot": "R", "min_w": 160},
        ]},
    },
    {
        # Tuile nue (image pure) — remplace l'ex-option par-tuile « __none__ / classique forcé ».
        "id": "builtin:video-only",
        "name": "Vidéo seule",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
             "fit": "fill", "border": "none"},
        ]},
    },
    {
        # Bezel « moniteur » : l'ex-frame_style stylized, porté par la bordure du composant video.
        "id": "builtin:monitor",
        "name": "Moniteur (bezel)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.02, "y": 0.03, "w": 0.96, "h": 0.85,
             "fit": "fill", "border": "stylized", "border_w": 4},
            {"id": "lampL", "type": "tally", "x": 0.04, "y": 0.905, "w": 0.045, "h": 0.075,
             "shape": "lamp", "slot": "L", "min_w": 180},
            {"id": "umd", "type": "umd", "x": 0.15, "y": 0.895, "w": 0.70, "h": 0.10,
             "text_source": "name", "bg_color": "", "bg_opacity": 0},
            {"id": "lampR", "type": "tally", "x": 0.915, "y": 0.905, "w": 0.045, "h": 0.075,
             "shape": "lamp", "slot": "R", "min_w": 180},
        ]},
    },
    {
        "id": "builtin:production",
        "name": "Production (UMD + tally)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
             "fit": "fill"},
            {"id": "lampL", "type": "tally", "x": 0.02, "y": 0.875, "w": 0.055, "h": 0.10,
             "shape": "lamp", "slot": "L", "min_w": 180},
            {"id": "umd", "type": "umd", "x": 0.14, "y": 0.865, "w": 0.72, "h": 0.125,
             "text_source": "name", "tally_bg": True, "bg_color": "#000000", "bg_opacity": 75},
            {"id": "lampR", "type": "tally", "x": 0.925, "y": 0.875, "w": 0.055, "h": 0.10,
             "shape": "lamp", "slot": "R", "min_w": 180},
            {"id": "onair", "type": "text", "x": 0.35, "y": 0.02, "w": 0.30, "h": 0.09,
             "text": "ON AIR", "when": "tally_red", "min_w": 240,
             "color": "#ffffff", "bg_color": "#cc0000", "bg_opacity": 85},
        ]},
    },
    {
        "id": "builtin:engineering",
        "name": "Ingénierie (format + ANC + VU)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 0.9, "h": 0.9,
             "fit": "fill"},
            {"id": "vu", "type": "meters", "x": 0.9, "y": 0.0, "w": 0.1, "h": 0.86,
             "channels": 2, "scale": "dbfs", "opacity": 100, "align": "right"},
            {"id": "fmt", "type": "format", "x": 0.5, "y": 0.015, "w": 0.385, "h": 0.075,
             "min_w": 260, "bg_color": "#000000", "bg_opacity": 65},
            {"id": "anc", "type": "anc", "x": 0.0, "y": 0.785, "w": 0.9, "h": 0.075,
             "anc_types": True, "anc_tc": True, "anc_crc": True, "anc_opacity": 60,
             "min_w": 320},
            {"id": "umd", "type": "umd", "x": 0.1, "y": 0.87, "w": 0.7, "h": 0.115,
             "text_source": "name", "tally_bg": True, "bg_color": "#000000", "bg_opacity": 75},
        ]},
    },
    {
        "id": "builtin:minimal",
        "name": "Minimal (vidéo + nom)",
        "builtin": True,
        "config": {"components": [
            {"id": "video", "type": "video", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
             "fit": "fill"},
            {"id": "umd", "type": "umd", "x": 0.25, "y": 0.88, "w": 0.5, "h": 0.10,
             "text_source": "name", "bg_color": "#000000", "bg_opacity": 55, "min_w": 120},
        ]},
    },
]
# NB : le modèle d'usine `builtin:audio-only` (cellule audio seule, "fenêtre déguisée") a été
# RETIRÉ en 0.36.0 — remplacé par le bloc VU-mètres de MUR (deploy_config.params.meter_blocks,
# cf. plugins/multiview/script.py render_meters). `resolve_pip_template` renvoie None pour un id
# absent : un mur dont une fenêtre référence encore cet id (`template_ref`) continue de s'afficher
# normalement, le moteur lisant le `template` déjà RÉSOLU et embarqué dans flux_config[i] (jamais
# l'id à l'exécution) ; seul le composer perd la capacité de RE-résoudre ce ref (bouton
# « ↻ Recharger »), avec repli propre déjà prévu (`_pipTemplateOptions` garde une option orpheline
# pour un `template_ref` absent de la bibliothèque).


def all_pip_templates():
    """Bibliothèque complète : modèles d'usine (builtin) + modèles DB, pour /api/pip_templates.
    Les builtins n'ont ni date de modification ni tags utilisateur — normalisés ici (`tags: []`,
    `updated_at: None`) pour que la galerie (static/pip_editor.js) ait un contrat uniforme sans
    deviner une fausse date sur les modèles d'usine."""
    from .database import db_get_pip_templates
    builtins = [dict(t, tags=[], updated_at=None) for t in BUILTIN_PIP_TEMPLATES]
    return builtins + db_get_pip_templates()


def resolve_pip_template(ref):
    """id (int DB ou 'builtin:…') → {"name", "components"[, "aspect"]} prêt à embarquer dans
    flux_config[i].template, ou None si introuvable. `aspect` = format LIBRE du modèle (ratio
    L/H de la cellule cible, absent = 16:9 implicite des modèles historiques) — miroir de
    mwResolveTemplate (multiview.js), qui fait la même résolution côté composer."""
    for t in all_pip_templates():
        if str(t.get("id")) == str(ref):
            cfg = t.get("config") or {}
            out = {"name": t.get("name") or "", "components": cfg.get("components") or []}
            try:
                a = float(cfg.get("aspect") or 0)
                if 0.1 < a < 10:
                    out["aspect"] = a
            except (TypeError, ValueError):
                pass
            return out
    return None
