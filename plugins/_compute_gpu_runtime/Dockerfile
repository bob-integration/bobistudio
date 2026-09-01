# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Image runtime « compute-GPU » pour Bobi.Studio — variante NVIDIA de l'image compute.
#
# Principe : FINE COUCHE par-dessus l'image compute EXACTE (FROM bobi-compute:<ver>) + cupy.
# → parité garantie (même libmxl/bobimxl/agent.py/numpy/PIL/polices que le chemin CPU), AUCUN
#   rebuild du libmxl (le builder vcpkg ~30 min n'est pas refait). Le même script.py de plugin
#   s'exécute ici (cupy présent → chemin GPU) ou sur l'image compute CPU (cupy absent → numpy),
#   via l'abstraction `xp` auto-détectée côté plugin (cf. multiview Phase 3).
#
# GPU : runtime NVIDIA fourni par nvidia-container-toolkit au `docker run --gpus` (libcuda.so
# injecté par le toolkit). cupy-cuda12x[ctk] embarque nvrtc/cudart/cublas/curand en WHEELS pip
# → AUCUNE base CUDA lourde nécessaire, l'image reste sur debian trixie slim (base compute).
# Validé au banc (dl360-2 / Tesla T4) : JIT cupy OK sur base slim + --gpus (driver 550, CUDA 12.4).
#
# Build (node_only — buildé SUR le nœud GPU, comme bobi-mtl ; l'image lourde reste locale, pas
# de push flotte) : l'image de base bobi-compute:<ver> doit être présente sur le nœud (elle l'est,
# distribuée par capacité). Contexte = racine du repo (cohérent avec les autres _IMAGES).
#   docker build -t bobi-compute-gpu:<ver> -f plugins/_compute_gpu_runtime/Dockerfile .
#
# COUPLAGE DE VERSION : BASE_IMAGE suit l'image_tag de plugins/_compute_runtime/meta.json.
# Tout bump de bobi-compute ⇒ mettre à jour ce défaut (et le meta.json voisin).
ARG BASE_IMAGE=bobi-compute:0.23
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
# cupy ÉPINGLÉ (déterminisme : un rebuild dans 6 mois = même CUDA). cupy-cuda12x==13.4.1 embarque
# le runtime CUDA 12.8 → exige un driver >= 570 (dell-1 : driver 580.105.08, cf. mémoire
# gpu-image-cuda-pin-vs-driver-floor). NE PAS laisser cupy-cuda12x non épinglé : py3.13 tire alors
# un CUDA récent qui casse en silence sur un driver trop vieux (repli numpy muet).
# ★ Les libs CUDA (nvrtc/cudart/cublas/…) doivent être installées EXPLICITEMENT : l'extra [ctk]
#   n'installe PAS les wheels nvidia-*-cu12 pour ce cupy (import nvidia échoue → libnvrtc.so.12
#   introuvable). Et cupy 13.x ne scanne PAS les dossiers des wheels → on les enregistre via
#   ld.so.conf.d + ldconfig (plus propre et permanent qu'un LD_LIBRARY_PATH par-conteneur).
# --break-system-packages : trixie marque l'environnement « externally-managed » (PEP 668).
ARG CUPY_VERSION=13.4.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && pip3 install --no-cache-dir --break-system-packages "cupy-cuda12x==${CUPY_VERSION}" \
      nvidia-cuda-nvrtc-cu12 nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 \
      nvidia-curand-cu12 nvidia-cusparse-cu12 nvidia-cusolver-cu12 \
      nvidia-cufft-cu12 nvidia-nvjitlink-cu12 \
 && python3 -c "import nvidia,os,glob;d=sorted(set(os.path.dirname(p) for p in glob.glob(os.path.dirname(nvidia.__file__)+'/**/lib/*.so*',recursive=True)));open('/etc/ld.so.conf.d/nvidia-wheels.conf','w').write(chr(10).join(d)+chr(10))" \
 && ldconfig \
 && apt-get purge -y python3-pip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /root/.cache

# Hérite ENTRYPOINT/EXPOSE/PYTHONPATH de l'image compute (agent.py :8081 ; script :8080/:8082).

# Kernels CUDA du compositing DVE (plugin `split`, chemin render=gpu). Compilés à chaud par NVRTC
# au premier usage, comme les kernels cupy du multiview. Le fichier vit dans script_templates/
# (à côté de mvcompose.c) et NON dans le script du plugin : script.py est un template str.format,
# où chaque accolade littérale de code CUDA devrait être doublée — piège garanti.
COPY script_templates/split_gpu.cu /usr/local/lib/mxl-python/split_gpu.cu

# Conformité de redistribution : ces images embarquent des binaires MXL (Apache-2.0) et, pour
# certaines, MTL (BSD-3-Clause). Les deux licences exigent qu'une COPIE du texte accompagne
# le binaire redistribué — pas seulement une mention. Voir licenses/README.md.
COPY licenses/ /usr/share/doc/bobistudio/licenses/
COPY THIRD-PARTY-NOTICES.md /usr/share/doc/bobistudio/THIRD-PARTY-NOTICES.md

