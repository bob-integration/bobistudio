***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Image runtime GÉNÉRIQUE « compute » pour Bobi.Studio — fait tourner N'IMPORTE QUEL plugin de
***REMOVED*** calcul (color_corrector, mixer, dve, multiview, avsync…) sous Docker, sur un nœud du cluster.
***REMOVED***
***REMOVED*** Principe : le conteneur embarque l'AGENT par-conteneur (script_templates/agent.py, le même
***REMOVED*** contrat :8081 deploy/start/stop/status que les LXC). L'orchestrateur y pousse le script.py rendu
***REMOVED*** (str.format) exactement comme pour un LXC → aucune image par plugin, pas de rebuild par modif.
***REMOVED*** Le script déployé sert lui-même :8080 (métriques) et :8082 (contrôle). /dev/shm est bind-monté
***REMOVED*** depuis l'hôte (`docker run -v <mxl_mount>:/dev/shm`) → le conteneur rejoint le pipeline MXL.
***REMOVED***
***REMOVED*** Réseau : macvlan/ipvlan (IP propre par conteneur) → :8080/:8081/:8082 fixes, comme un LXC.
***REMOVED*** Pas de --privileged, pas de hugepages, pas de DPDK/XDP : calcul numpy/Pillow pur.
***REMOVED***
***REMOVED*** CHANTIER MXL — PHASE 0 : cette image embarque désormais libmxl (SDK Linux Foundation) +
***REMOVED*** bobimxl.py (binding ctypes). Stratégie HYBRIDE par-plugin (cf. plan) : les plugins de
***REMOVED*** traitement numpy adoptent MXL via bobimxl (Python), sans changer le modèle « script poussé ».
***REMOVED*** Tant que la Phase 1 (migration plugin) n'est pas lancée, libmxl/bobimxl sont présents mais
***REMOVED*** inertes (aucun script de prod ne les importe) → bump non disruptif.
***REMOVED***
***REMOVED*** Build (contexte = RACINE du repo, pour pouvoir COPY script_templates/) :
***REMOVED***   docker build -t bobi-compute:0.10 -f plugins/_compute_runtime/Dockerfile .  (fabrics RDMA + v210)
***REMOVED*** (build via l'agent-nœud en prod : node_driver.build_image — pas de root-SSH.)

***REMOVED*** ---------------------------------------------------------------- étage builder : compiler libmxl
***REMOVED*** Reproduit le schéma de build amont (examples/Dockerfile : vcpkg + preset CMake + ninja).
FROM debian:trixie-slim AS mxl-builder
ARG MXL_REF=v1.1.0
ENV DEBIAN_FRONTEND=noninteractive
ENV VCPKG_ROOT=/opt/vcpkg
***REMOVED*** build-essential/cmake/ninja + deps de build des ports vcpkg (autoconf/automake/libtool/nasm/
***REMOVED*** bison/flex) — liste dérivée du .devcontainer amont. CHANTIER RDMA : les Fabrics OFI sont
***REMOVED*** désormais ACTIVÉES (-DMXL_ENABLE_FABRICS_OFI=ON) → libfabric requis au build (le CMake
***REMOVED*** fabrics fait pkg_check_modules(libfabric REQUIRED ...)). Produit en plus libmxl-fabrics.so +
***REMOVED*** l'outil mxl-fabrics-demo (réplication de flux inter-hôtes, cf. app/rdma.py). GStreamer/Rust
***REMOVED*** restent NON requis.
***REMOVED***
***REMOVED*** ⚠ PAS de `libfabric-dev` : cf. l'étage libfabric ci-dessous. On garde en revanche
***REMOVED*** libibverbs-dev/librdmacm-dev, que `libfabric-dev` tirait implicitement et dont le provider
***REMOVED*** VERBS (RoCEv2 = notre réplication RDMA) a besoin AU BUILD.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential cmake ninja-build pkg-config \
      autoconf automake autoconf-archive libtool nasm bison flex libpcap-dev \
      libibverbs-dev librdmacm-dev \
      curl zip unzip tar \
 && rm -rf /var/lib/apt/lists/*

***REMOVED*** ─── libfabric COMPILÉE DEPUIS LES SOURCES (pas le paquet de la distribution) ────────────────
***REMOVED*** MXL v1.1.0-rc1 utilise `FI_SOCKADDR_IP` (lib/fabrics/ofi/src/internal/FabricAddress.hpp), absent
***REMOVED*** de la libfabric 2.1.0 de Debian trixie → la compilation échoue net (« FI_SOCKADDR_IP was not
***REMOVED*** declared in this scope », plus un `duplicate case value` qui en découle). Mesuré le 2026-08-13
***REMOVED*** sur dell-1 : c'est ce qui a fait échouer les 4 images au premier bump vers rc1.
***REMOVED***
***REMOVED*** L'AMONT NE SE SERT PAS NON PLUS DU PAQUET : `.devcontainer/scripts/common/libfabric/install.sh`
***REMOVED*** compile v2.5.1 depuis git — y compris dans son propre `Dockerfile.debiantrixie`. On reproduit ce
***REMOVED*** script à l'identique (mêmes drapeaux `configure`), seul le préfixe change : /opt/libfabric au lieu
***REMOVED*** de /usr, pour que l'étage runtime n'ait qu'un dossier précis à recopier.
ARG LIBFABRIC_REF=v2.5.1
RUN git clone --depth 1 --branch "${LIBFABRIC_REF}" https://github.com/ofiwg/libfabric.git /src/libfabric \
 && cd /src/libfabric \
 && ./autogen.sh \
 && ./configure --prefix=/opt/libfabric \
      --disable-kdreg2 --disable-memhooks-monitor --disable-uffd-monitor \
 && make -j"$(nproc)" install \
 && rm -rf /src/libfabric

***REMOVED*** ⚠ GARDE-FOU ANTI-PANNE SILENCIEUSE, dans son propre RUN (le tordre ne doit pas relancer la
***REMOVED*** compilation ci-dessus) : sans libibverbs-dev, `configure` désactive le provider VERBS SANS
***REMOVED*** erreur — libfabric se construit, l'image se construit, et la réplication RDMA tombe en TCP (ou
***REMOVED*** ne trouve aucune interface) SEULEMENT en production.
***REMOVED*** On INTERROGE la lib construite (`fi_info -l` énumère les providers réellement embarqués) au lieu
***REMOVED*** d'inférer depuis les sources ou de fouiller le binaire : un `strings | grep '^verbs$'` a été
***REMOVED*** essayé le 2026-08-13 et donnait un FAUX NÉGATIF (le nom du provider n'est pas une chaîne isolée),
***REMOVED*** faisant échouer un build parfaitement bon.
RUN LD_LIBRARY_PATH=/opt/libfabric/lib /opt/libfabric/bin/fi_info -l > /tmp/fi_providers.txt 2>&1 \
 && grep -qw verbs /tmp/fi_providers.txt \
 || { echo "ERREUR: provider VERBS absent de libfabric — RoCEv2/RDMA serait mort en silence."; \
      echo "--- providers réellement embarqués ---"; cat /tmp/fi_providers.txt; false; }
***REMOVED*** pkg_check_modules(libfabric) du CMake MXL doit voir NOTRE libfabric, pas celle du système.
ENV PKG_CONFIG_PATH=/opt/libfabric/lib/pkgconfig
***REMOVED*** Clone COMPLET (pas --depth 1) : vcpkg.json épingle un builtin-baseline qu'un clone shallow
***REMOVED*** ne contient pas (git show <baseline>:versions/baseline.json échoue sinon, sur TOUS les ports).
RUN git clone https://github.com/microsoft/vcpkg.git "$VCPKG_ROOT" \
 && "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
***REMOVED*** Sources MXL épinglées (ARG MXL_REF) → reproductibilité ; bump = changer le tag.
RUN git clone --depth 1 --branch "${MXL_REF}" https://github.com/dmf-mxl/mxl.git /src/mxl
WORKDIR /src/mxl
***REMOVED*** CONFIGURE séparé du build (couche CACHÉE) : la configuration cmake déclenche l'install vcpkg
***REMOVED*** (compile toutes les deps, ~30 min) — on l'isole pour que les itérations du patch ci-dessous
***REMOVED*** ne la relancent pas. On N'UTILISE PAS le preset (toolchain ~/vcpkg hardcodé + générateur
***REMOVED*** capricieux) : cmake explicite. vcpkg en mode manifeste (vcpkg.json) via le toolchain file.
***REMOVED*** ─── DEUX NIVEAUX D'INSTRUCTIONS (glibc-hwcaps) ──────────────────────────────────────────────
***REMOVED*** libmxl était bâtie avec l'architecture NATIVE de la machine de build : elle embarquait donc du
***REMOVED*** BMI2/AVX2 et tombait en SIGILL sur tout CPU antérieur à Haswell. Mesuré le 2026-08-01 : les trois
***REMOVED*** R620 (Sandy Bridge) du parc n'exécutaient AUCUN conteneur du pipeline — l'agent relançait le
***REMOVED*** script en boucle et l'orchestrateur n'affichait qu'un « script_stopped » sans cause.
***REMOVED***
***REMOVED*** La baseline seule ne suffit pas non plus : mesuré à charge identique sur dell-1, un CONSOMMATEUR
***REMOVED*** (mixer, lecture+écriture du ring) coûte +28 % en x86-64-v2 qu'en v3 (17,4 % → 22,2 % d'un cœur,
***REMOVED*** quatre séries concordantes). Un producteur, lui, ne perd que ~2 % : c'est en mesurant les DEUX
***REMOVED*** régimes que le compromis est apparu — le premier verdict, tiré du seul producteur, disait « écart
***REMOVED*** nul » et aurait fait livrer une baseline unique.
***REMOVED***
***REMOVED*** D'où le double build. La BASELINE va au chemin normal (elle s'exécute partout) et la variante
***REMOVED*** OPTIMISÉE dans `glibc-hwcaps/x86-64-v3/` : l'éditeur de liens dynamique la choisit tout seul sur
***REMOVED*** un CPU capable, sans une ligne de code d'aiguillage ni sélection d'image par nœud. Coût : un
***REMOVED*** second passage du build long.
***REMOVED***
***REMOVED*** ⚠ Le WRAPPER de compilateur est indispensable. Passer `-DCMAKE_C_FLAGS=-march=…` est SANS EFFET
***REMOVED*** (vérifié : libmxl ressortait bit pour bit identique) — ces drapeaux passent AVANT ceux que la
***REMOVED*** cible ajoute, et le `-march` du CMake amont ou du triplet vcpkg gagne. Le wrapper met `-march`
***REMOVED*** en DERNIER argument ; gcc retient la dernière occurrence.
***REMOVED*** ⚠ `\$@` est échappé (il doit rester LITTÉRAL dans le wrapper) mais `$a` ne l'est PAS : c'est la
***REMOVED*** variable de la boucle, elle doit être remplacée ICI. Les échapper toutes les deux produisait un
***REMOVED*** `-march=x86-64-` vide → le test de compilateur de cmake échouait et le configure s'arrêtait sur un
***REMOVED*** `exit code 1` dont la cause réelle n'apparaissait nulle part. D'où le contrôle explicite ci-dessous :
***REMOVED*** le wrapper est AFFICHÉ et ESSAYÉ, l'étage échoue tout de suite s'il est malformé.
RUN for a in v2 v3; do \
      { echo '***REMOVED***!/bin/sh'; echo "exec /usr/bin/gcc \"\$@\" -march=x86-64-$a"; } > /usr/local/bin/cc-$a; \
      { echo '***REMOVED***!/bin/sh'; echo "exec /usr/bin/g++ \"\$@\" -march=x86-64-$a"; } > /usr/local/bin/cxx-$a; \
      chmod +x /usr/local/bin/cc-$a /usr/local/bin/cxx-$a; \
    done \
 && tail -1 /usr/local/bin/cc-v2 && tail -1 /usr/local/bin/cc-v3 \
 && /usr/local/bin/cc-v2 -x c -c /dev/null -o /dev/null \
 && /usr/local/bin/cc-v3 -x c -c /dev/null -o /dev/null
ENV MXL_CMAKE_ARGS="-G Ninja -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
 -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DBUILD_TOOLS=ON \
 -DBUILD_DOCS=OFF -DBUILD_TESTS=OFF -DBUILD_UTILS=OFF -DMXL_ENABLE_FABRICS_OFI=ON"
***REMOVED*** CONFIGURE de la baseline (couche cachée : c'est elle qui déclenche l'install vcpkg, ~30 min).
RUN cmake -S . -B build $MXL_CMAKE_ARGS \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_C_COMPILER=/usr/local/bin/cc-v2 -DCMAKE_CXX_COMPILER=/usr/local/bin/cxx-v2 \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install \
      || { echo "===== vcpkg-manifest-install.log ====="; \
           tail -n 120 /src/mxl/build/vcpkg-manifest-install.log 2>/dev/null; \
           echo "===== derniers logs de ports ====="; \
           for f in $(ls -t /opt/vcpkg/buildtrees/*/*-err.log 2>/dev/null | head -3); do \
             echo "--- $f ---"; tail -n 60 "$f"; done; false; }
***REMOVED*** Patch bobi.studio : media_type vidéo PLANAR (video/x-mxl-planar). Appliqué APRÈS le configure
***REMOVED*** (ne touche que FlowParser.cpp) → seul `cmake --build` recompile (rapide). Local en attendant
***REMOVED*** la PR upstream ; généré contre le tag MXL_REF (cf. aide « MXL (bus shm) & format vidéo »).
COPY plugins/_compute_runtime/patches/mxl-planar-type.patch /tmp/mxl-planar-type.patch
COPY plugins/_compute_runtime/patches/mxl-planar-slices.patch /tmp/mxl-planar-slices.patch
***REMOVED*** Curseur de tranche non réinitialisé au changement de grain dans l'initiateur fabrics : bandes
***REMOVED*** venant d'une autre trame sur toute réplique de flux TRANCHÉ. ⚠ C'est CETTE image que font tourner
***REMOVED*** les conteneurs rdma-ini/rdma-tgt (cf. services/rdma._image_for_node) — patcher la seule image du
***REMOVED*** moteur ne corrigerait RIEN. Cf. docs/chantiers/MXL_FABRICS_SLICE_CORRUPTION.md.
COPY plugins/_compute_runtime/patches/mxl-fabrics-slice-reset.patch /tmp/mxl-fabrics-slice-reset.patch
***REMOVED*** Notification (donnée immédiate) clôturée derrière la charge utile qu'elle annonce :
***REMOVED*** sans FI_FENCE rien n'ordonne les écritures RMA d'un lot multi-plans, la cible publie des
***REMOVED*** tranches dont les octets sont encore en vol. SECONDE cause des bandes déchirées.
COPY plugins/_compute_runtime/patches/mxl-fabrics-fence-notification.patch /tmp/mxl-fabrics-fence-notification.patch
RUN git apply --verbose /tmp/mxl-planar-type.patch \
 && git apply --verbose /tmp/mxl-planar-slices.patch \
 && git apply --verbose /tmp/mxl-fabrics-slice-reset.patch \
 && git apply --verbose /tmp/mxl-fabrics-fence-notification.patch \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build
***REMOVED*** Variante OPTIMISÉE (x86-64-v3 = AVX2 + FMA + BMI2). Arbre déjà patché, dépendances vcpkg déjà
***REMOVED*** bâties → seule la compilation de MXL est refaite. Architecture ÉPINGLÉE, pas native : le résultat
***REMOVED*** ne dépend plus de la machine de build (le build natif produisait de l'AVX-512 sur un Xeon récent,
***REMOVED*** du v3 ailleurs — et personne ne pouvait dire ce que contenait l'image livrée).
RUN cmake -S . -B build-v3 $MXL_CMAKE_ARGS \
      -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
      -DCMAKE_C_COMPILER=/usr/local/bin/cc-v3 -DCMAKE_CXX_COMPILER=/usr/local/bin/cxx-v3 \
      -DCMAKE_INSTALL_PREFIX=/opt/mxl-install-v3 \
 && cmake --build build-v3 -j"$(nproc)" \
 && cmake --install build-v3

***REMOVED*** ------------------------------------------------- étage builder : convertisseur v210 (interop)
***REMOVED*** Pack/unpack v210↔planar en C auto-vectorisé (chantier interop MXL, cf. MXL_INTEROP.md
***REMOVED*** §Mise à jour 2026-07-12 — le ~33 ms/hop numpy tombe à ~4-5 ms en SIMD). Deux variantes car
***REMOVED*** l'image est build-once+push vers des nœuds hétérogènes : baseline x86-64-v2 (toujours sûre)
***REMOVED*** + x86-64-v3 (AVX2, Haswell+) — bobimxl charge la v3 si /proc/cpuinfo annonce avx2.
***REMOVED*** ────────────────────────────────────────────── étage builder : binaires DeckLink (plugin decklink_io)
***REMOVED*** ★ RIEN DE BLACKMAGIC N'EST EMBARQUÉ ICI. Seuls les EN-TÊTES du SDK entrent (leur licence
***REMOVED***   autorise la redistribution par un tiers, cf. THIRD-PARTY-NOTICES.md) ; `libDeckLinkAPI.so`
***REMOVED***   est chargée à l'EXÉCUTION et montée depuis l'hôte, le pilote Desktop Video s'installant à la
***REMOVED***   main sur le nœud. Un nœud sans carte porte donc ces binaires sans pouvoir les utiliser — ils
***REMOVED***   pèsent quelques centaines de kilo-octets et ne démarrent que si un decklink_io est déployé.
FROM debian:trixie-slim AS decklink-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
      g++ libssl-dev && rm -rf /var/lib/apt/lists/*
COPY --from=mxl-builder /opt/mxl-install/lib/ /usr/local/lib/
COPY --from=mxl-builder /opt/mxl-install/include/ /usr/local/include/
RUN ldconfig
COPY plugins/decklink_io/decklink_sdk /src/decklink_sdk
COPY plugins/decklink_io/anc_rfc8331.h /src/anc_rfc8331.h
COPY plugins/decklink_io/decklink_mxl.cpp /src/decklink_mxl.cpp
COPY plugins/decklink_io/decklink_tx.cpp /src/decklink_tx.cpp
WORKDIR /src
RUN for b in decklink_mxl decklink_tx; do \
      g++ -O2 -Wall -Wextra -Wno-deprecated-declarations -std=c++11 -fno-rtti \
        -I/src -I/usr/local/include -o /$b $b.cpp decklink_sdk/DeckLinkAPIDispatch.cpp \
        -L/usr/local/lib -lmxl -lcrypto -ldl -lpthread || exit 1; \
    done

FROM debian:trixie-slim AS v210-builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY script_templates/v210convert.c /src/v210convert.c
RUN mkdir -p /out \
 && gcc -O3 -march=x86-64-v2 -shared -fPIC /src/v210convert.c -o /out/libbobi_v210.so \
 && gcc -O3 -march=x86-64-v3 -shared -fPIC /src/v210convert.c -o /out/libbobi_v210_v3.so
***REMOVED*** Kernel de compositing multiview fusionné (chantier fusion numpy→C 2026-07-12) : blend/
***REMOVED*** blend_pre/place/rgba2yuv en UNE passe mémoire (le compose CPU est memory-bound). Mêmes
***REMOVED*** variantes v2/v3 que le v210 ; OpenMP (-fopenmp) → libgomp1 requis dans l'image finale.
***REMOVED*** -ffp-contract=off OBLIGATOIRE : rgba2yuv doit reproduire les arrondis float32 de numpy
***REMOVED*** (une contraction FMA changerait les octets — le bit-exact du selftest en dépend).
COPY script_templates/mvcompose.c /src/mvcompose.c
RUN gcc -O3 -march=x86-64-v2 -fopenmp -ffp-contract=off -shared -fPIC /src/mvcompose.c -o /out/libbobi_mvk.so -lm \
 && gcc -O3 -march=x86-64-v3 -fopenmp -ffp-contract=off -shared -fPIC /src/mvcompose.c -o /out/libbobi_mvk_v3.so -lm
***REMOVED*** Copie NON TEMPORELLE de l'écriture de grain (bobimxl.blit). UNE SEULE variante : le choix
***REMOVED*** SSE2/AVX2 est fait à l'EXÉCUTION (__builtin_cpu_supports) et non par -march, sans quoi le .so
***REMOVED*** lèverait SIGILL sur les R620 Sandy Bridge. bobimxl CALIBRE au premier usage et retombe sur
***REMOVED*** memcpy si le non temporel ne gagne pas sur ce nœud (il PERD de 20 % sur dl360-1) — le .so
***REMOVED*** absent est donc sans conséquence, et sa présence ne peut pas dégrader.
COPY script_templates/ntcopy.c /src/ntcopy.c
RUN gcc -O3 -march=x86-64-v2 -shared -fPIC /src/ntcopy.c -o /out/libbobi_nt.so

***REMOVED*** Noyaux de mesure du plugin `scope` (waveform, vecteur-scope, extrêmes). Mêmes variantes v2/v3 que
***REMOVED*** le v210 et le mvk. PAS de `-march=native` ni de SIMD : la boucle est bornée par des écritures
***REMOVED*** DISPERSÉES dans l'histogramme, elle ne se vectorise pas — mesuré 2,63 ms en 1080 sur un cœur
***REMOVED*** Cascade Lake, contre 12 à 18 ms en numpy (docs/chantiers/SCOPE.md §3).
COPY script_templates/scopekernel.c /src/scopekernel.c
RUN gcc -O3 -march=x86-64-v2 -shared -fPIC /src/scopekernel.c -o /out/libbobi_scope.so \
 && gcc -O3 -march=x86-64-v3 -shared -fPIC /src/scopekernel.c -o /out/libbobi_scope_v3.so

***REMOVED*** ---------------------------------------------------------------- image finale : runtime compute
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
***REMOVED*** Set de polices curaté pour les overlays texte/horloge du multiview (choix de police dans
***REMOVED*** l'UI). FONT_MAP du script.py pointe sur ces chemins ; repli DejaVu si une police manque.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-pil ca-certificates libstdc++6 libgomp1 \
      fonts-dejavu-core fonts-liberation fonts-inter fonts-roboto fonts-firacode \
      rdma-core ibverbs-providers libibverbs1 librdmacm1 \
 && rm -rf /var/lib/apt/lists/*

***REMOVED*** libfabric : celle qu'on a COMPILÉE (v2.5.1), surtout pas `libfabric1` d'apt (2.1.0 sur trixie).
***REMOVED*** Les deux portent le même soname `libfabric.so.1` : garder le paquet ferait charger l'ANCIENNE au
***REMOVED*** runtime — libmxl-fabrics.so se lierait sans erreur puis manquerait des symboles, la panne se
***REMOVED*** déplaçant simplement de la compilation vers l'exécution. rdma-core/ibverbs-providers restent
***REMOVED*** d'apt : ils fournissent libibverbs/librdmacm, dont le provider verbs dépend (ABI stable).
***REMOVED***
***REMOVED*** ⚠ `libibverbs1`/`librdmacm1` sont listés EXPLICITEMENT ci-dessus, et ce n'est pas de la
***REMOVED*** redondance : c'est `libfabric1` qui les tirait transitivement. En le retirant on a perdu
***REMOVED*** librdmacm — mesuré le 2026-08-13, `ldd libmxl-fabrics.so` rendait « librdmacm.so.1 => not
***REMOVED*** found » sur une image qui s'était construite SANS UNE ERREUR. Ne pas les enlever.
COPY --from=mxl-builder /opt/libfabric/lib/libfabric.so* /usr/local/lib/

***REMOVED*** libmxl + libmxl-common (libmxl.so dépend de libmxl-common.so.1 — copier TOUT lib/) + outils
***REMOVED*** mxl-info / mxl-fabrics-demo (réplication RDMA inter-hôtes). Copier TOUT lib/ ramène aussi
***REMOVED*** libmxl-fabrics.so (lie libfabric → d'où libfabric1/rdma-core ci-dessus). ldconfig résout les deps.
***REMOVED*** (Validé au banc : deps restantes = libstdc++6/libgcc_s/libc/libm = libs système présentes.)
COPY --from=mxl-builder /opt/mxl-install/lib/ /usr/local/lib/
COPY --from=mxl-builder /opt/mxl-install/bin/ /usr/local/bin/
***REMOVED*** Variante AVX2/FMA/BMI2, choisie AUTOMATIQUEMENT par l'éditeur de liens sur un CPU capable
***REMOVED*** (glibc ≥ 2.33 — trixie l'a). Sur un CPU antérieur, le sous-dossier est simplement ignoré et la
***REMOVED*** baseline du chemin normal s'applique : aucun aiguillage à écrire, aucune image par nœud.
***REMOVED*** ⚠ Les OUTILS (bin/) restent en baseline : un exécutable ne bénéficie pas de hwcaps. C'est sans
***REMOVED*** conséquence — `mxl-fabrics-demo` coûte 1,2 % d'un cœur, indépendamment du débit (mesuré), il est
***REMOVED*** borné par le DMA de la carte, pas par le CPU.
COPY --from=mxl-builder /opt/mxl-install-v3/lib/ /usr/local/lib/glibc-hwcaps/x86-64-v3/
***REMOVED*** Convertisseur v210↔planar (interop MXL) + kernel compose multiview fusionné (libbobi_mvk)
***REMOVED*** — chargés par bobimxl (ctypes), repli numpy sinon. /out/ porte les 4 .so (v2 + v3).
COPY --from=v210-builder /out/ /usr/local/lib/
COPY --from=decklink-builder /decklink_mxl /usr/local/bin/decklink_mxl
COPY --from=decklink-builder /decklink_tx /usr/local/bin/decklink_tx
RUN ldconfig

***REMOVED*** ⚠ GARDE-FOU DE L'ÉTAGE RUNTIME — l'étage builder ne prouve RIEN sur l'image livrée.
***REMOVED*** Vécu le 2026-08-13 : le garde-fou `verbs` du builder passait, MXL compilait, l'image se
***REMOVED*** construisait sans une erreur… et `ldd libmxl-fabrics.so` rendait « librdmacm.so.1 => not found »,
***REMOVED*** parce que retirer `libfabric1` d'apt avait supprimé une dépendance qu'il tirait transitivement.
***REMOVED*** La réplication RDMA serait morte au premier lien, en production, sans aucun signal au build.
***REMOVED*** On exige donc que TOUTES les dépendances dynamiques du chemin RDMA soient résolues DANS L'IMAGE.
RUN if ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep -q "not found"; then \
      echo "ERREUR: dépendance dynamique NON RÉSOLUE dans l'image runtime (chemin RDMA) :"; \
      ldd /usr/local/lib/libmxl-fabrics.so /usr/local/bin/mxl-fabrics-demo 2>&1 | grep "not found"; \
      false; \
    fi

***REMOVED*** Agent par-conteneur (contrat :8081). Copié verbatim depuis script_templates/ → pas de drift.
COPY script_templates/agent.py /usr/local/bin/agent.py
***REMOVED*** bobimxl : binding ctypes de libmxl, sur le sys.path des scripts déployés (PYTHONPATH hérité
***REMOVED*** par le subprocess que l'agent spawne). Même vecteur qu'agent.py → un seul exemplaire partagé.
COPY script_templates/bobimxl.py /usr/local/lib/mxl-python/bobimxl.py
ENV PYTHONPATH=/usr/local/lib/mxl-python
RUN mkdir -p /opt/script /var/log

***REMOVED*** :8081 agent · :8080 métriques (script) · :8082 contrôle (script)
EXPOSE 8081 8080 8082

***REMOVED*** Conformité de redistribution : ces images embarquent des binaires MXL (Apache-2.0) et, pour
***REMOVED*** certaines, MTL (BSD-3-Clause). Les deux licences exigent qu'une COPIE du texte accompagne
***REMOVED*** le binaire redistribué — pas seulement une mention. Voir licenses/README.md.
COPY licenses/ /usr/share/doc/bobistudio/licenses/
COPY THIRD-PARTY-NOTICES.md /usr/share/doc/bobistudio/THIRD-PARTY-NOTICES.md

ENTRYPOINT ["python3", "-u", "/usr/local/bin/agent.py"]
