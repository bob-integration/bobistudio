***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED***
***REMOVED*** Image de BANC (pas un runtime de production) : libmxl **STOCK**, SANS nos patches
***REMOVED*** (`mxl-planar-type` / `mxl-planar-slices`) — c'est-à-dire exactement ce qu'embarque un
***REMOVED*** container d'un AUTRE éditeur. Sert au **banc croisé stock↔fork** du chantier interop
***REMOVED*** (cf. MXL_INTEROP.md §« Déjà conforme (à confirmer au banc croisé) ») :
***REMOVED***
***REMOVED***   - le `mxl-info` stock DOIT lire notre flow miroir `video/v210` (publié par le plugin
***REMOVED***     v210_bridge) → preuve que l'interop vidéo passe ;
***REMOVED***   - le `mxl-info` stock DOIT rejeter proprement un flow `video/x-mxl-planar` → preuve
***REMOVED***     que le type maison est bien le point de rupture (et qu'il ne doit jamais être exposé).
***REMOVED***
***REMOVED*** Les étages ci-dessous reproduisent VOLONTAIREMENT à l'identique les couches de
***REMOVED*** plugins/_compute_runtime/Dockerfile jusqu'au `cmake` de configuration (mêmes FROM/ENV/RUN,
***REMOVED*** au caractère près) → le cache Docker du nœud de build est réutilisé (vcpkg ~30 min évité).
***REMOVED*** La SEULE divergence est l'absence de `git apply` des patches avant `cmake --build`.
***REMOVED***
***REMOVED*** Build (contexte = RACINE du repo) :
***REMOVED***   docker build -t bobi-mxl-stock:1.1.0 -f plugins/_mxl_stock_bench/Dockerfile .
***REMOVED***
***REMOVED*** ⚠ Le TAG porte le tag amont (`MXL_REF`), sinon un `bobi-mxl-stock:1.1` déjà présent sur le nœud
***REMOVED*** de banc serait silencieusement l'ANCIENNE libmxl : on croirait comparer au stock courant en
***REMOVED*** comparant à celui d'il y a deux mois. Bumper `MXL_REF` = bumper ce tag.

FROM debian:trixie-slim AS mxl-builder
ARG MXL_REF=v1.1.0
ENV DEBIAN_FRONTEND=noninteractive
ENV VCPKG_ROOT=/opt/vcpkg
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential cmake ninja-build pkg-config \
      autoconf automake autoconf-archive libtool nasm bison flex libpcap-dev \
      libibverbs-dev librdmacm-dev \
      curl zip unzip tar \
 && rm -rf /var/lib/apt/lists/*

***REMOVED*** ─── libfabric COMPILÉE DEPUIS LES SOURCES ───────────────────────────────────────────────────
***REMOVED*** Bloc identique à _compute_runtime (cache partagé, cf. en-tête) : MXL rc1 exige `FI_SOCKADDR_IP`,
***REMOVED*** absent de la libfabric 2.1.0 de Debian trixie. NB : ceci ne rompt PAS le principe de cette image
***REMOVED*** — libmxl reste STOCK (aucun `git apply`), seule sa DÉPENDANCE libfabric est provisionnée comme
***REMOVED*** l'amont le fait lui-même. Un tiers qui suit le README amont a exactement la même.
ARG LIBFABRIC_REF=v2.5.1
RUN git clone --depth 1 --branch "${LIBFABRIC_REF}" https://github.com/ofiwg/libfabric.git /src/libfabric \
 && cd /src/libfabric \
 && ./autogen.sh \
 && ./configure --prefix=/opt/libfabric \
      --disable-kdreg2 --disable-memhooks-monitor --disable-uffd-monitor \
 && make -j"$(nproc)" install \
 && rm -rf /src/libfabric

RUN LD_LIBRARY_PATH=/opt/libfabric/lib /opt/libfabric/bin/fi_info -l > /tmp/fi_providers.txt 2>&1 \
 && grep -qw verbs /tmp/fi_providers.txt \
 || { echo "ERREUR: provider VERBS absent de libfabric — RoCEv2/RDMA serait mort en silence."; \
      echo "--- providers réellement embarqués ---"; cat /tmp/fi_providers.txt; false; }
ENV PKG_CONFIG_PATH=/opt/libfabric/lib/pkgconfig
RUN git clone https://github.com/microsoft/vcpkg.git "$VCPKG_ROOT" \
 && "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
RUN git clone --depth 1 --branch "${MXL_REF}" https://github.com/dmf-mxl/mxl.git /src/mxl
WORKDIR /src/mxl
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
      -DBUILD_SHARED_LIBS=ON -DBUILD_TOOLS=ON \
      -DBUILD_DOCS=OFF -DBUILD_TESTS=OFF -DBUILD_UTILS=OFF \
      -DMXL_ENABLE_FABRICS_OFI=ON \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install \
      || { echo "===== vcpkg-manifest-install.log ====="; \
           tail -n 120 /src/mxl/build/vcpkg-manifest-install.log 2>/dev/null; \
           echo "===== derniers logs de ports ====="; \
           for f in $(ls -t /opt/vcpkg/buildtrees/*/*-err.log 2>/dev/null | head -3); do \
             echo "--- $f ---"; tail -n 60 "$f"; done; false; }
***REMOVED*** ⚠ AUCUN `git apply` ici — c'est TOUT l'intérêt de cette image (libmxl stock, comme un tiers).
RUN cmake --build build -j"$(nproc)" \
 && cmake --install build

***REMOVED*** ---------------------------------------------------------------- image finale : banc stock
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
***REMOVED*** gcc/libc6-dev : pour compiler la SONDE stock (stock_probe.c) contre les headers stock —
***REMOVED*** `mxl-info` ne lit que le descripteur shm et affiche N'IMPORTE quel flow ; seul un vrai
***REMOVED*** mxlCreateFlowReader() emprunte le parseur de flow_def qui rejette un media_type inconnu.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-numpy ca-certificates libstdc++6 \
      gcc libc6-dev \
      rdma-core ibverbs-providers libibverbs1 librdmacm1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=mxl-builder /opt/mxl-install/lib/ /usr/local/lib/
COPY --from=mxl-builder /opt/mxl-install/bin/ /usr/local/bin/
COPY --from=mxl-builder /opt/mxl-install/include/ /usr/local/include/
***REMOVED*** libfabric compilée (v2.5.1), pas `libfabric1` d'apt — même soname, l'ancienne serait chargée.
***REMOVED*** `libibverbs1`/`librdmacm1` explicites : `libfabric1` les tirait transitivement.
COPY --from=mxl-builder /opt/libfabric/lib/libfabric.so* /usr/local/lib/
RUN ldconfig

***REMOVED*** Garde-fou de l'étage RUNTIME : une dépendance non résolue rendrait ce banc MENTEUR (on croirait
***REMOVED*** éprouver l'interop stock alors que le chemin RDMA ne se charge même pas). Cf. _compute_runtime.
RUN if ldd /usr/local/lib/libmxl-fabrics.so 2>&1 | grep -q "not found"; then \
      echo "ERREUR: dépendance dynamique NON RÉSOLUE dans l'image de banc (chemin RDMA) :"; \
      ldd /usr/local/lib/libmxl-fabrics.so 2>&1 | grep "not found"; \
      false; \
    fi
***REMOVED*** Sonde STOCK : consommateur vidéo écrit contre le SDK non patché (comme un tiers).
COPY plugins/_mxl_stock_bench/stock_probe.c /src/stock_probe.c
RUN gcc -O2 /src/stock_probe.c -o /usr/local/bin/stock_probe -lmxl
***REMOVED*** Le banc lance ses commandes en override d'entrypoint.
ENTRYPOINT ["/bin/bash", "-lc"]
