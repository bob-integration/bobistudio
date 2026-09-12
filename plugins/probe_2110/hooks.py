# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
#
# probe_2110 — hooks lifecycle (exécutés IN-PROCESS, mais sans handles DB/token : uniquement
# un dict `params` + un contexte minimal, cf. app/plugins.py). La sonde réutilise la
# normalisation du receiver 2110_io (format vidéo, comptes RX) puis FORCE le profil « mesure » :
# RX-only strict (aucune sortie TX/ANC), parser de conformité activé, PF vfio dédiée.

import logging

log = logging.getLogger(__name__)


def before_deploy(params, context):
    """Normalise + verrouille le profil sonde. Appelé par docker_driver.deploy_docker (chemin MTL)
    AVANT la construction de la ligne `docker run`. Idempotent."""
    try:
        from app.scripts import normalize_receiver_params
        params = normalize_receiver_params(params, settings=context.get("settings"))
    except Exception as e:  # normalisation best-effort — ne jamais bloquer un deploy sonde
        log.warning("probe_2110 before_deploy: normalize_receiver_params ignoré (%s)", e)

    # Profil MESURE, non négociable : une sonde est un RECEIVER pur. On coupe toute émission
    # (TX/ANC) pour ne consommer QUE des files RX sur la PF dédiée et n'exposer aucun sender NMOS.
    params["probe_mode"] = True
    params["timing_parser"] = True
    params["tx_count"] = 0
    params["active_tx_count"] = 0
    params["anc_count"] = 0
    # tx_slots/tx_flows n'ont aucun sens pour une sonde : les vider (rétro-compat si recopiés).
    params["tx_slots"] = []
    params["tx_flows"] = []

    # Slots RX : une sonde analyse UN flux à la fois par port en Phase A. On garde ≥1 slot vidéo.
    try:
        vc = int(params.get("video_count") or 0)
    except (TypeError, ValueError):
        vc = 0
    if vc < 1:
        params["video_count"] = 1
    try:
        arc = int(params.get("active_rx_count") or 0)
    except (TypeError, ValueError):
        arc = 0
    if arc < 1:
        params["active_rx_count"] = 1

    # Conformité audio (ST 2110-30) optionnelle : n'allouer un slot audio RX que si demandé.
    if params.get("measure_audio"):
        try:
            if int(params.get("audio_count") or 0) < 1:
                params["audio_count"] = 1
        except (TypeError, ValueError):
            params["audio_count"] = 1
    else:
        params["audio_count"] = 0

    return params
