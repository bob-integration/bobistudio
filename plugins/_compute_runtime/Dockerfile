# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Image runtime GÉNÉRIQUE « compute » pour Bobi.Studio — fait tourner N'IMPORTE QUEL plugin de
# calcul (color_corrector, mixer, dve, multiview, avsync…) sous Docker, sur un nœud du cluster.
#
# Principe : le conteneur embarque l'AGENT par-conteneur (script_templates/agent.py, le même
# contrat :8081 deploy/start/stop/status que les LXC). L'orchestrateur y pousse le script.py rendu
# (str.format) exactement comme pour un LXC → aucune image par plugin, pas de rebuild par modif.
# Le script déployé sert lui-même :8080 (métriques) et :8082 (contrôle). /dev/shm est bind-monté
# depuis l'hôte (`docker run -v <mxl_mount>:/dev/shm`) → le conteneur rejoint le pipeline MXL.
#
# Réseau : macvlan/ipvlan (IP propre par conteneur) → :8080/:8081/:8082 fixes, comme un LXC.
# Pas de --privileged, pas de hugepages, pas de DPDK/XDP : calcul numpy/Pillow pur.
#
# CHANTIER MXL — PHASE 0 : cette image embarque désormais libmxl (SDK Linux Foundation) +
# bobimxl.py (binding ctypes). Stratégie HYBRIDE par-plugin (cf. plan) : les plugins de
# traitement numpy adoptent MXL via bobimxl (Python), sans changer le modèle « script poussé ».
# Tant que la Phase 1 (migration plugin) n'est pas lancée, libmxl/bobimxl sont présents mais
# inertes (aucun script de prod ne les importe) → bump non disruptif.
#
# Build (contexte = RACINE du repo, pour pouvoir COPY script_templates/) :
#   docker build -t bobi-compute:0.10 -f plugins/_compute_runtime/Dockerfile .  (fabrics RDMA + v210)
# (build via l'agent-nœud en prod : node_driver.build_image — pas de root-SSH.)

# ---------------------------------------------------------------- étage builder : compiler libmxl
# Reproduit le schéma de build amont (examples/Dockerfile : vcpkg + preset CMake + ninja).
FROM debian:trixie-slim AS mxl-builder
ARG MXL_REF=v1.1.0-rc1
ENV DEBIAN_FRONTEND=noninteractive
ENV VCPKG_ROOT=/opt/vcpkg
# build-essential/cmake/ninja + deps de build des ports vcpkg (autoconf/automake/libtool/nasm/
# bison/flex) — liste dérivée du .devcontainer amont. CHANTIER RDMA : les Fabrics OFI sont
# désormais ACTIVÉES (-DMXL_ENABLE_FABRICS_OFI=ON) → libfabric requis au build (le CMake
# fabrics fait pkg_check_modules(libfabric REQUIRED ...)). Produit en plus libmxl-fabrics.so +
# l'outil mxl-fabrics-demo (réplication de flux inter-hôtes, cf. app/rdma.py). GStreamer/Rust
# restent NON requis.
#
# ⚠ PAS de `libfabric-dev` : cf. l'étage libfabric ci-dessous. On garde en revanche
# libibverbs-dev/librdmacm-dev, que `libfabric-dev` tirait implicitement et dont le provider
# VERBS (RoCEv2 = notre réplication RDMA) a besoin AU BUILD.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential cmake ninja-build pkg-config \
      autoconf automake autoconf-archive libtool nasm bison flex libpcap-dev \
      libibverbs-dev librdmacm-dev \
      curl zip unzip tar \
 && rm -rf /var/lib/apt/lists/*

# ─── libfabric COMPILÉE DEPUIS LES SOURCES (pas le paquet de la distribution) ────────────────
# MXL v1.1.0-rc1 utilise `FI_SOCKADDR_IP` (lib/fabrics/ofi/src/internal/FabricAddress.hpp), absent
# de la libfabric 2.1.0 de Debian trixie → la compilation échoue net (« FI_SOCKADDR_IP was not
# declared in this scope », plus un `duplicate case value` qui en découle). Mesuré le 2026-08-13
# sur dell-1 : c'est ce qui a fait échouer les 4 images au premier bump vers rc1.
#
# L'AMONT NE SE SERT PAS NON PLUS DU PAQUET : `.devcontainer/scripts/common/libfabric/install.sh`
# compile v2.5.1 depuis git — y compris dans son propre `Dockerfile.debiantrixie`. On reproduit ce
# script à l'identique (mêmes drapeaux `configure`), seul le préfixe change : /opt/libfabric au lieu
# de /usr, pour que l'étage runtime n'ait qu'un dossier précis à recopier.
ARG LIBFABRIC_REF=v2.5.1
RUN git clone --depth 1 --branch "${LIBFABRIC_REF}" https://github.com/ofiwg/libfabric.git /src/libfabric \
 && cd /src/libfabric \
 && ./autogen.sh \
 && ./configure --prefix=/opt/libfabric \
      --disable-kdreg2 --disable-memhooks-monitor --disable-uffd-monitor \
 && make -j"$(nproc)" install \
 && rm -rf /src/libfabric

# ⚠ GARDE-FOU ANTI-PANNE SILENCIEUSE, dans son propre RUN (le tordre ne doit pas relancer la
# compilation ci-dessus) : sans libibverbs-dev, `configure` désactive le provider VERBS SANS
# erreur — libfabric se construit, l'image se construit, et la réplication RDMA tombe en TCP (ou
# ne trouve aucune interface) SEULEMENT en production.
# On INTERROGE la lib construite (`fi_info -l` énumère les providers réellement embarqués) au lieu
# d'inférer depuis les sources ou de fouiller le binaire : un `strings | grep '^verbs$'` a été
# essayé le 2026-08-13 et donnait un FAUX NÉGATIF (le nom du provider n'est pas une chaîne isolée),
# faisant échouer un build parfaitement bon.
RUN LD_LIBRARY_PATH=/opt/libfabric/lib /opt/libfabric/bin/fi_info -l > /tmp/fi_providers.txt 2>&1 \
 && grep -qw verbs /tmp/fi_providers.txt \
 || { echo "ERREUR: provider VERBS absent de libfabric — RoCEv2/RDMA serait mort en silence."; \
      echo "--- providers réellement embarqués ---"; cat /tmp/fi_providers.txt; false; }
# pkg_check_modules(libfabric) du CMake MXL doit voir NOTRE libfabric, pas celle du système.
ENV PKG_CONFIG_PATH=/opt/libfabric/lib/pkgconfig
# Clone COMPLET (pas --depth 1) : vcpkg.json épingle un builtin-baseline qu'un clone shallow
# ne contient pas (git show <baseline>:versions/baseline.json échoue sinon, sur TOUS les ports).
RUN git clone https://github.com/microsoft/vcpkg.git "$VCPKG_ROOT" \
 && "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
# Sources MXL épinglées (ARG MXL_REF) → reproductibilité ; bump = changer le tag.
RUN git clone --depth 1 --branch "${MXL_REF}" https://github.com/dmf-mxl/mxl.git /src/mxl
WORKDIR /src/mxl
# CONFIGURE séparé du build (couche CACHÉE) : la configuration cmake déclenche l'install vcpkg
# (compile toutes les deps, ~30 min) — on l'isole pour que les itérations du patch ci-dessous
# ne la relancent pas. On N'UTILISE PAS le preset (toolchain ~/vcpkg hardcodé + générateur
# capricieux) : cmake explicite. vcpkg en mode manifeste (vcpkg.json) via le toolchain file.
# ─── DEUX NIVEAUX D'INSTRUCTIONS (glibc-hwcaps) ──────────────────────────────────────────────
# libmxl était bâtie avec l'architecture NATIVE de la machine de build : elle embarquait donc du
# BMI2/AVX2 et tombait en SIGILL sur tout CPU antérieur à Haswell. Mesuré le 2026-08-01 : les trois
# R620 (Sandy Bridge) du parc n'exécutaient AUCUN conteneur du pipeline — l'agent relançait le
# script en boucle et l'orchestrateur n'affichait qu'un « script_stopped » sans cause.
#
# La baseline seule ne suffit pas non plus : mesuré à charge identique sur dell-1, un CONSOMMATEUR
# (mixer, lecture+écriture du ring) coûte +28 % en x86-64-v2 qu'en v3 (17,4 % → 22,2 % d'un cœur,
# quatre séries concordantes). Un producteur, lui, ne perd que ~2 % : c'est en mesurant les DEUX
# régimes que le compromis est apparu — le premier verdict, tiré du seul producteur, disait « écart
# nul » et aurait fait livrer une baseline unique.
#
# D'où le double build. La BASELINE va au chemin normal (elle s'exécute partout) et la variante
# OPTIMISÉE dans `glibc-hwcaps/x86-64-v3/` : l'éditeur de liens dynamique la choisit tout seul sur
# un CPU capable, sans une ligne de code d'aiguillage ni sélection d'image par nœud. Coût : un
# second passage du build long.
#
# ⚠ Le WRAPPER de compilateur est indispensable. Passer `-DCMAKE_C_FLAGS=-march=…` est SANS EFFET
# (vérifié : libmxl ressortait bit pour bit identique) — ces drapeaux passent AVANT ceux que la
# cible ajoute, et le `-march` du CMake amont ou du triplet vcpkg gagne. Le wrapper met `-march`
# en DERNIER argument ; gcc retient la dernière occurrence.
# ⚠ `\$@` est échappé (il doit rester LITTÉRAL dans le wrapper) mais `$a` ne l'est PAS : c'est la
# variable de la boucle, elle doit être remplacée ICI. Les échapper toutes les deux produisait un
# `-march=x86-64-` vide → le test de compilateur de cmake échouait et le configure s'arrêtait sur un
# `exit code 1` dont la cause réelle n'apparaissait nulle part. D'où le contrôle explicite ci-dessous :
# le wrapper est AFFICHÉ et ESSAYÉ, l'étage échoue tout de suite s'il est malformé.
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
# Patch bobi.studio : media_type vidéo PLANAR (video/x-mxl-planar). Appliqué APRÈS le configure
# (ne touche que FlowParser.cpp) → seul `cmake --build` recompile (rapide). Local en attendant
# la PR upstream ; généré contre le tag MXL_REF (cf. aide « MXL (bus shm) & format vidéo »).
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
# Variante OPTIMISÉE (x86-64-v3 = AVX2 + FMA + BMI2). Arbre déjà patché, dépendances vcpkg déjà
# bâties → seule la compilation de MXL est refaite. Architecture ÉPINGLÉE, pas native : le résultat
# ne dépend plus de la machine de build (le build natif produisait de l'AVX-512 sur un Xeon récent,
# du v3 ailleurs — et personne ne pouvait dire ce que contenait l'image livrée).
RUN cmake -S . -B build-v3 $MXL_CMAKE_ARGS \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_C_COMPILER=/usr/local/bin/cc-v3 -DCMAKE_CXX_COMPILER=/usr/local/bin/cxx-v3 \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install-v3 \
 && cmake --build build-v3 -j"$(nproc)" \
 && cmake --install build-v3

# ------------------------------------------------- étage builder : convertisseur v210 (interop)
# Pack/unpack v210↔planar en C auto-vectorisé (chantier interop MXL, cf. MXL_INTEROP.md
# §Mise à jour 2026-07-12 — le ~33 ms/hop numpy tombe à ~4-5 ms en SIMD). Deux variantes car
# l'image est build-once+push vers des nœuds hétérogènes : baseline x86-64-v2 (toujours sûre)
# + x86-64-v3 (AVX2, Haswell+) — bobimxl charge la v3 si /proc/cpuinfo annonce avx2.
FROM debian:trixie-slim AS v210-builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY script_templates/v210convert.c /src/v210convert.c
RUN mkdir -p /out \
 && gcc -O3 -march=x86-64-v2 -shared -fPIC /src/v210convert.c -o /out/libbobi_v210.so \
 && gcc -O3 -march=x86-64-v3 -shared -fPIC /src/v210convert.c -o /out/libbobi_v210_v3.so
# Kernel de compositing multiview fusionné (chantier fusion numpy→C 2026-07-12) : blend/
# blend_pre/place/rgba2yuv en UNE passe mémoire (le compose CPU est memory-bound). Mêmes
# variantes v2/v3 que le v210 ; OpenMP (-fopenmp) → libgomp1 requis dans l'image finale.
# -ffp-contract=off OBLIGATOIRE : rgba2yuv doit reproduire les arrondis float32 de numpy
# (une contraction FMA changerait les octets — le bit-exact du selftest en dépend).
COPY script_templates/mvcompose.c /src/mvcompose.c
RUN gcc -O3 -march=x86-64-v2 -fopenmp -ffp-contract=off -shared -fPIC /src/mvcompose.c -o /out/libbobi_mvk.so -lm \
 && gcc -O3 -march=x86-64-v3 -fopenmp -ffp-contract=off -shared -fPIC /src/mvcompose.c -o /out/libbobi_mvk_v3.so -lm
# Copie NON TEMPORELLE de l'écriture de grain (bobimxl.blit). UNE SEULE variante : le choix
# SSE2/AVX2 est fait à l'EXÉCUTION (__builtin_cpu_supports) et non par -march, sans quoi le .so
# lèverait SIGILL sur les R620 Sandy Bridge. bobimxl CALIBRE au premier usage et retombe sur
# memcpy si le non temporel ne gagne pas sur ce nœud (il PERD de 20 % sur dl360-1) — le .so
# absent est donc sans conséquence, et sa présence ne peut pas dégrader.
COPY script_templates/ntcopy.c /src/ntcopy.c
RUN gcc -O3 -march=x86-64-v2 -shared -fPIC /src/ntcopy.c -o /out/libbobi_nt.so

# Noyaux de mesure du plugin `scope` (waveform, vecteur-scope, extrêmes). Mêmes variantes v2/v3 que
# le v210 et le mvk. PAS de `-march=native` ni de SIMD : la boucle est bornée par des écritures
# DISPERSÉES dans l'histogramme, elle ne se vectorise pas — mesuré 2,63 ms en 1080 sur un cœur
# Cascade Lake, contre 12 à 18 ms en numpy (docs/chantiers/SCOPE.md §3).
COPY script_templates/scopekernel.c /src/scopekernel.c
RUN gcc -O3 -march=x86-64-v2 -shared -fPIC /src/scopekernel.c -o /out/libbobi_scope.so \
 && gcc -O3 -march=x86-64-v3 -shared -fPIC /src/scopekernel.c -o /out/libbobi_scope_v3.so

# ---------------------------------------------------------------- image finale : runtime compute
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
# Set de polices curaté pour les overlays texte/horloge du multiview (choix de police dans
# l'UI). FONT_MAP du script.py pointe sur ces chemins ; repli DejaVu si une police manque.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-pil ca-certificates libstdc++6 libgomp1 \
      fonts-dejavu-core fonts-liberation fonts-inter fonts-roboto fonts-firacode \
      rdma-core ibverbs-providers libibverbs1 librdmacm1 \
 && rm -rf /var/lib/apt/lists/*

# libfabric : celle qu'on a COMPILÉE (v2.5.1), surtout pas `libfabric1` d'apt (2.1.0 sur trixie).
# Les deux portent le même soname `libfabric.so.1` : garder le paquet ferait charger l'ANCIENNE au
# runtime — libmxl-fabrics.so se lierait sans erreur puis manquerait des symboles, la panne se
# déplaçant simplement de la compilation vers l'exécution. rdma-core/ibverbs-providers restent
# d'apt : ils fournissent libibverbs/librdmacm, dont le provider verbs dépend (ABI stable).
#
# ⚠ `libibverbs1`/`librdmacm1` sont listés EXPLICITEMENT ci-dessus, et ce n'est pas de la
# redondance : c'est `libfabric1` qui les tirait transitivement. En le retirant on a perdu
# librdmacm — mesuré le 2026-08-13, `ldd libmxl-fabrics.so` rendait « librdmacm.so.1 => not
# found » sur une image qui s'était construite SANS UNE ERREUR. Ne pas les enlever.
COPY --from=mxl-builder /opt/libfabric/lib/libfabric.so* /usr/local/lib/

# libmxl + libmxl-common (libmxl.so dépend de libmxl-common.so.1 — copier TOUT lib/) + outils
# mxl-info / mxl-fabrics-demo (réplication RDMA inter-hôtes). Copier TOUT lib/ ramène aussi
# libmxl-fabrics.so (lie libfabric → d'où libfabric1/rdma-core ci-dessus). ldconfig résout les deps.
# (Validé au banc : deps restantes = libstdc++6/libgcc_s/libc/libm = libs système présentes.)
COPY --from=mxl-builder /opt/mxl-install/lib/ /usr/local/lib/
COPY --from=mxl-builder /opt/mxl-install/bin/ /usr/local/bin/
# Variante AVX2/FMA/BMI2, choisie AUTOMATIQUEMENT par l'éditeur de liens sur un CPU capable
# (glibc ≥ 2.33 — trixie l'a). Sur un CPU antérieur, le sous-dossier est simplement ignoré et la
# baseline du chemin normal s'applique : aucun aiguillage à écrire, aucune image par nœud.
# ⚠ Les OUTILS (bin/) restent en baseline : un exécutable ne bénéficie pas de hwcaps. C'est sans
# conséquence — `mxl-fabrics-demo` coûte 1,2 % d'un cœur, indépendamment du débit (mesuré), il est
# borné par le DMA de la carte, pas par le CPU.
COPY --from=mxl-builder /opt/mxl-install-v3/lib/ /usr/local/lib/glibc-hwcaps/x86-64-v3/
# Convertisseur v210↔planar (interop MXL) + kernel compose multiview fusionné (libbobi_mvk)
# — chargés par bobimxl (ctypes), repli numpy sinon. /out/ porte les 4 .so (v2 + v3).
COPY --from=v210-builder /out/ /usr/local/lib/
RUN ldconfig

# ⚠ GARDE-FOU DE L'ÉTAGE RUNTIME — l'étage builder ne prouve RIEN sur l'image livrée.
# Vécu le 2026-08-13 : le garde-fou `verbs` du builder passait, MXL compilait, l'image se
# construisait sans une erreur… et `ldd libmxl-fabrics.so` rendait « librdmacm.so.1 => not found »,
# parce que retirer `libfabric1` d'apt avait supprimé une dépendance qu'il tirait transitivement.
# La réplication RDMA serait morte au premier lien, en production, sans aucun signal au build.
# On exige donc que TOUTES les dépendances dynamiques du chemin RDMA soient résolues DANS L'IMAGE.
RUN if ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep -q "not found"; then \
      echo "ERREUR: dépendance dynamique NON RÉSOLUE dans l'image runtime (chemin RDMA) :"; \
      ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep "not found"; \
      false; \
    fi

# Agent par-conteneur (contrat :8081). Copié verbatim depuis script_templates/ → pas de drift.
COPY script_templates/agent.py /usr/local/bin/agent.py
# bobimxl : binding ctypes de libmxl, sur le sys.path des scripts déployés (PYTHONPATH hérité
# par le subprocess que l'agent spawne). Même vecteur qu'agent.py → un seul exemplaire partagé.
COPY script_templates/bobimxl.py /usr/local/lib/mxl-python/bobimxl.py
ENV PYTHONPATH=/usr/local/lib/mxl-python
RUN mkdir -p /opt/script /var/log

# :8081 agent · :8080 métriques (script) · :8082 contrôle (script)
EXPOSE 8081 8080 8082

# Conformité de redistribution : ces images embarquent des binaires MXL (Apache-2.0) et, pour
# certaines, MTL (BSD-3-Clause). Les deux licences exigent qu'une COPIE du texte accompagne
# le binaire redistribué — pas seulement une mention. Voir licenses/README.md.
COPY licenses/ /usr/share/doc/bobistudio/licenses/
COPY THIRD-PARTY-NOTICES.md /usr/share/doc/bobistudio/THIRD-PARTY-NOTICES.md

ENTRYPOINT ["python3", "-u", "/usr/local/bin/agent.py"]
