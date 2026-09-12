# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

def before_deploy(params, context):
    """Dérive les compteurs de wiring depuis le sens du pont (motif delay) :
    - export : 1 entrée vidéo câblable (le planar interne à refléter), 0 sortie interne
      (le miroir v210 est pour les TIERS — nos readers attendent du planar) ;
    - import : 0 entrée (la source est un flux v210 tiers, hors topologie maison),
      1 sortie planar interne {hostname} câblable normalement."""
    params = dict(params)
    is_import = str(params.get("direction") or "export").strip().lower() == "import"
    params["direction"]       = "import" if is_import else "export"
    params["consumes_video"]  = 0 if is_import else 1
    params["produces_planar"] = 1 if is_import else 0
    return params
