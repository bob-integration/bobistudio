# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Image JETABLE du banc plugins tiers, étape 0 (docs/chantiers/CONSOLE_PLUGINS_TIERS_BANC0.md).
# Construite et supprimée sur un nœud de banc ; jamais poussée, jamais installée sur le contrôleur.
# Uniquement des paquets libres de Debian trixie (main).
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config liblilv-dev lv2-dev lilv-utils python3 python3-lilv procps \
      x42-plugins lsp-plugins-lv2 calf-plugins zam-plugins dragonfly-reverb-lv2 ardour-lv2-plugins \
      eq10q invada-studio-plugins-lv2 swh-lv2 mda-lv2 ir.lv2 abgate fomp dpf-plugins-lv2 \
      bankstown-lv2 rubberband-lv2 \
    && apt-get clean
WORKDIR /banc
COPY plugcore.c host.c engine.c shm.h Makefile run_bench.py campaign.sh ./
RUN make
