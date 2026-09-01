# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Image runtime « média » pour Bobi.Studio — variante de bobi-compute pour les plugins MÉDIA qui
# ont besoin d'un accès STOCKAGE et d'un moteur lourd : player / recorder (GStreamer, temps réel),
# transcoder (ffmpeg, batch fichier) et streamer (ffmpeg → UDP/SRT/WebRTC).
#
# Même contrat que bobi-compute : embarque l'AGENT par-conteneur (script_templates/agent.py, :8081
# deploy/start/stop/status), l'orchestrateur y pousse le script.py rendu. Le script sert :8080
# (métriques) et :8082 (contrôle). /dev/shm est bind-monté (`-v <mxl_mount>:/dev/shm`) → pipeline MXL.
# En plus de bobi-compute : le volume média est bind-monté (`-v <media_mount>/<projet>:/mnt/media`).
#
# CHANTIER MXL : embarque libmxl + bobimxl (comme bobi-compute) pour les plugins média migrés
# (streamer = consommateur vidéo MXL). L'étage builder est IDENTIQUE à celui de bobi-compute →
# BuildKit réutilise les couches en cache (pas de recompil vcpkg si compute déjà buildé).
#
# Build (contexte = RACINE du repo) :
#   docker build -t bobi-media:0.6 -f plugins/_media_runtime/Dockerfile .   (fabrics RDMA)

# ---------------------------------------------------------------- étage builder : compiler libmxl
# ⚠ DOIT rester byte-identique au builder de plugins/_compute_runtime/Dockerfile (cache partagé).
FROM debian:trixie-slim AS mxl-builder
ARG MXL_REF=v1.1.0-rc1
ENV DEBIAN_FRONTEND=noninteractive
ENV VCPKG_ROOT=/opt/vcpkg
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential cmake ninja-build pkg-config \
      autoconf automake autoconf-archive libtool nasm bison flex libpcap-dev \
      libibverbs-dev librdmacm-dev \
      curl zip unzip tar \
 && rm -rf /var/lib/apt/lists/*

# ─── libfabric COMPILÉE DEPUIS LES SOURCES ───────────────────────────────────────────────────
# ⚠ Bloc byte-identique à celui de plugins/_compute_runtime/Dockerfile (cache partagé) — voir
# là-bas le détail : MXL rc1 exige `FI_SOCKADDR_IP`, absent de la libfabric 2.1.0 de Debian
# trixie, et l'amont compile lui aussi v2.5.1 depuis git dans son propre Dockerfile.debiantrixie.
ARG LIBFABRIC_REF=v2.5.1
RUN git clone --depth 1 --branch "${LIBFABRIC_REF}" https://github.com/ofiwg/libfabric.git /src/libfabric \
 && cd /src/libfabric \
 && ./autogen.sh \
 && ./configure --prefix=/opt/libfabric \
      --disable-kdreg2 --disable-memhooks-monitor --disable-uffd-monitor \
 && make -j"$(nproc)" install \
 && rm -rf /src/libfabric

# Garde-fou : le provider VERBS (RoCEv2) doit être RÉELLEMENT embarqué, sinon le RDMA meurt en
# silence en production. On interroge la lib construite, on n'infère pas (cf. _compute_runtime).
RUN LD_LIBRARY_PATH=/opt/libfabric/lib /opt/libfabric/bin/fi_info -l > /tmp/fi_providers.txt 2>&1 \
 && grep -qw verbs /tmp/fi_providers.txt \
 || { echo "ERREUR: provider VERBS absent de libfabric — RoCEv2/RDMA serait mort en silence."; \
      echo "--- providers réellement embarqués ---"; cat /tmp/fi_providers.txt; false; }
ENV PKG_CONFIG_PATH=/opt/libfabric/lib/pkgconfig
RUN git clone https://github.com/microsoft/vcpkg.git "$VCPKG_ROOT" \
 && "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
RUN git clone --depth 1 --branch "${MXL_REF}" https://github.com/dmf-mxl/mxl.git /src/mxl
WORKDIR /src/mxl
RUN for a in v2 v3; do \
      { echo '#!/bin/sh'; echo "exec /usr/bin/gcc \"\$@\" -march=x86-64-$a"; } > /usr/local/bin/cc-$a; \
      { echo '#!/bin/sh'; echo "exec /usr/bin/g++ \"\$@\" -march=x86-64-$a"; } > /usr/local/bin/cxx-$a; \
      chmod +x /usr/local/bin/cc-$a /usr/local/bin/cxx-$a; \
    done \
 && tail -1 /usr/local/bin/cc-v2 && tail -1 /usr/local/bin/cc-v3 \
 && /usr/local/bin/cc-v2 -x c -c /dev/null -o /dev/null \
 && /usr/local/bin/cc-v3 -x c -c /dev/null -o /dev/null
ENV MXL_CMAKE_ARGS="-G Ninja -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
 -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DBUILD_TOOLS=ON \
 -DBUILD_DOCS=OFF -DBUILD_TESTS=OFF -DBUILD_UTILS=OFF -DMXL_ENABLE_FABRICS_OFI=ON"
# CONFIGURE de la baseline (couche cachée : c'est elle qui déclenche l'install vcpkg, ~30 min).
RUN cmake -S . -B build $MXL_CMAKE_ARGS \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_C_COMPILER=/usr/local/bin/cc-v2 -DCMAKE_CXX_COMPILER=/usr/local/bin/cxx-v2 \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install \
      || { echo "===== vcpkg-manifest-install.log ====="; \
           tail -n 120 /src/mxl/build/vcpkg-manifest-install.log 2>/dev/null; \
           echo "===== derniers logs de ports ====="; \
           for f in $(ls -t /opt/vcpkg/buildtrees/*/*-err.log 2>/dev/null | head -3); do \
             echo "--- $f ---"; tail -n 60 "$f"; done; false; }
COPY plugins/_compute_runtime/patches/mxl-planar-type.patch /tmp/mxl-planar-type.patch
COPY plugins/_compute_runtime/patches/mxl-planar-slices.patch /tmp/mxl-planar-slices.patch
# Curseur de tranche non réinitialisé au changement de grain dans l'initiateur fabrics : bandes
# venant d'une autre trame sur toute réplique de flux TRANCHÉ. ⚠ C'est CETTE image que font tourner
# les conteneurs rdma-ini/rdma-tgt (cf. services/rdma._image_for_node) — patcher la seule image du
# moteur ne corrigerait RIEN. Cf. docs/chantiers/MXL_FABRICS_SLICE_CORRUPTION.md.
COPY plugins/_compute_runtime/patches/mxl-fabrics-slice-reset.patch /tmp/mxl-fabrics-slice-reset.patch
# Notification (donnée immédiate) clôturée derrière la charge utile qu'elle annonce :
# sans FI_FENCE rien n'ordonne les écritures RMA d'un lot multi-plans, la cible publie des
# tranches dont les octets sont encore en vol. SECONDE cause des bandes déchirées.
COPY plugins/_compute_runtime/patches/mxl-fabrics-fence-notification.patch /tmp/mxl-fabrics-fence-notification.patch
RUN git apply --verbose /tmp/mxl-planar-type.patch \
 && git apply --verbose /tmp/mxl-planar-slices.patch \
 && git apply --verbose /tmp/mxl-fabrics-slice-reset.patch \
 && git apply --verbose /tmp/mxl-fabrics-fence-notification.patch \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build
RUN cmake -S . -B build-v3 $MXL_CMAKE_ARGS \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_C_COMPILER=/usr/local/bin/cc-v3 -DCMAKE_CXX_COMPILER=/usr/local/bin/cxx-v3 \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install-v3 \
 && cmake --build build-v3 -j"$(nproc)" \
 && cmake --install build-v3
# Copie non temporelle de l'écriture de grain — variante UNIQUE (aiguillage SSE2/AVX2 à
# l'exécution, sinon SIGILL sur Sandy Bridge). Construite ici : l'étage a déjà gcc.
COPY script_templates/ntcopy.c /src/ntcopy.c
RUN mkdir -p /out && gcc -O3 -march=x86-64-v2 -shared -fPIC /src/ntcopy.c -o /out/libbobi_nt.so

# ---------------------------------------------------------------- image finale : runtime média
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
# - python3-gi + gstreamer1.0-* : moteur GStreamer (player/recorder), bindings GObject Python.
#   plugins-base/good/bad/ugly/libav couvrent decodebin, x264, prores/dnxhd (via libav), qtmux/
#   matroskamux/splitmuxsink, appsrc/appsink, audioconvert, timecode (GstVideoTimeCodeMeta).
# - ffmpeg : transcoder (batch fichier) + streamer (UDP/SRT/WebRTC) + repli thumbnails.
# - numpy/pil : empaquetage shm / vignettes. libstdc++6 : runtime de libmxl.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-pil python3-gi ca-certificates libstdc++6 \
      ffmpeg \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      rdma-core ibverbs-providers libibverbs1 librdmacm1 \
 && rm -rf /var/lib/apt/lists/*

# libmxl + libmxl-common + outils mxl-info/mxl-fabrics-demo + libmxl-fabrics.so (cf. bobi-compute).
# libfabric = celle qu'on a COMPILÉE, pas `libfabric1` d'apt (2.1.0, même soname → chargerait
# l'ancienne au runtime). `libibverbs1`/`librdmacm1` sont EXPLICITES : c'est `libfabric1` qui les
# tirait transitivement, et sans eux `ldd libmxl-fabrics.so` rend « librdmacm.so.1 => not found »
# sur une image construite sans une erreur (mesuré le 2026-08-13). Ne pas les enlever.
COPY --from=mxl-builder /opt/mxl-install/lib/ /usr/local/lib/
COPY --from=mxl-builder /opt/mxl-install-v3/lib/ /usr/local/lib/glibc-hwcaps/x86-64-v3/
COPY --from=mxl-builder /opt/mxl-install/bin/ /usr/local/bin/
COPY --from=mxl-builder /opt/libfabric/lib/libfabric.so* /usr/local/lib/
RUN ldconfig

# Garde-fou de l'étage RUNTIME (l'étage builder ne prouve rien sur l'image livrée) : toutes les
# dépendances dynamiques du chemin RDMA doivent être résolues DANS L'IMAGE. Cf. _compute_runtime.
RUN if ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep -q "not found"; then \
      echo "ERREUR: dépendance dynamique NON RÉSOLUE dans l'image runtime (chemin RDMA) :"; \
      ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep "not found"; \
      false; \
    fi

# Agent par-conteneur (contrat :8081). Copié verbatim depuis script_templates/ → pas de drift.
COPY script_templates/agent.py /usr/local/bin/agent.py
# bobimxl : binding ctypes libmxl, sur le PYTHONPATH des scripts déployés (idem bobi-compute).
COPY script_templates/bobimxl.py /usr/local/lib/mxl-python/bobimxl.py
# Copie non temporelle de l'écriture de grain (bobimxl.blit) — cf. bobi-compute pour le pourquoi
# de la variante UNIQUE (aiguillage SSE2/AVX2 à l'exécution) et de la calibration par nœud.
# Construite dans l'étage mxl-builder, qui a déjà gcc.
COPY --from=mxl-builder /out/libbobi_nt.so /usr/local/lib/libbobi_nt.so
ENV PYTHONPATH=/usr/local/lib/mxl-python
RUN mkdir -p /opt/script /var/log /mnt/media

# :8081 agent · :8080 métriques (script) · :8082 contrôle (script)
EXPOSE 8081 8080 8082

# Conformité de redistribution : ces images embarquent des binaires MXL (Apache-2.0) et, pour
# certaines, MTL (BSD-3-Clause). Les deux licences exigent qu'une COPIE du texte accompagne
# le binaire redistribué — pas seulement une mention. Voir licenses/README.md.
COPY licenses/ /usr/share/doc/bobistudio/licenses/
COPY THIRD-PARTY-NOTICES.md /usr/share/doc/bobistudio/THIRD-PARTY-NOTICES.md

ENTRYPOINT ["python3", "-u", "/usr/local/bin/agent.py"]
