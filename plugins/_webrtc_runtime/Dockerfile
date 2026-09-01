# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Image runtime « WebRTC » pour Bobi.Studio — passerelle MediaMTX sous Docker (macvlan), sur un
# nœud du cluster. Sœur de bobi-compute/bobi-media, MAIS avec le binaire MediaMTX PRÉ-BAKÉ
# (plus de téléchargement au 1er lancement → démarrage immédiat et hors-ligne).
#
# Principe identique aux autres images compute : le conteneur embarque l'AGENT par-conteneur
# (script_templates/agent.py, contrat :8081 deploy/start/stop/status). L'orchestrateur y pousse le
# script.py rendu (services/webrtc_gateway/script.py) qui lance mediamtx et sert :8080 (métriques).
# ensure_binary() du script détecte /usr/local/bin/mediamtx déjà présent → aucun download.
#
# Réseau : macvlan (IP propre) → :8554 (RTSP ingest) · :8889 (WHEP/WHIP) · :9997 (API) · :8081
# (agent) · :8080 (métriques) directement sur l'IP du conteneur, comme un LXC.
#
# Build (contexte = RACINE du repo, pour COPY script_templates/agent.py) :
#   docker build -t bobi-webrtc:0.1 -f plugins/_webrtc_runtime/Dockerfile .
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
ARG MTX_VERSION=1.9.3
ARG TARGETARCH=amd64

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# MediaMTX PRÉ-BAKÉ (binaire statique) — pinné à MTX_VERSION. Au même chemin que celui attendu par
# services/webrtc_gateway/script.py (BIN_PATH=/usr/local/bin/mediamtx) → ensure_binary() le détecte
# et saute le téléchargement runtime.
RUN curl -fsSL \
      "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_VERSION}/mediamtx_v${MTX_VERSION}_linux_${TARGETARCH}.tar.gz" \
      -o /tmp/mediamtx.tar.gz \
 && tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
 && chmod 0755 /usr/local/bin/mediamtx \
 && rm -f /tmp/mediamtx.tar.gz \
 && /usr/local/bin/mediamtx --version || true

# Agent par-conteneur (contrat :8081). Copié verbatim depuis script_templates/ → pas de drift.
COPY script_templates/agent.py /usr/local/bin/agent.py
RUN mkdir -p /opt/script /var/log

# :8081 agent · :8080 métriques · :8554 RTSP · :8889 WHEP/WHIP · :9997 API MediaMTX
EXPOSE 8081 8080 8554 8889 9997
ENTRYPOINT ["python3", "-u", "/usr/local/bin/agent.py"]
