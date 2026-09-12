# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Profondeur de la FENÊTRE LISIBLE d'un flux à grains — et pourquoi elle a changé.

MXL v1.1.0 (commit amont `6d5ac9c6`, « Fix discrete reader/writer tail conflict ») avance la
borne basse du lecteur de `headIndex - grainCount + 1` à `+ 2`, pour réserver la case de queue à
l'écrivain : la queue et `headIndex+1` désignent la même case PHYSIQUE de l'anneau, donc un
lecteur posé sur la plus vieille case pouvait voir l'état d'écriture non commité de la suivante.

⚠ « Discrete » en MXL veut dire GRAINS — vidéo et ANC. PAS l'audio, qui est un flux CONTINU et
avait déjà ce comportement ; le message du commit dit qu'il « aligne le discret sur le continu ».
Se tromper là-dessus fait chercher un effet dans la mauvaise essence.

CE QUE CE BANC MESURE, et c'est le seul contrôle qui distingue proprement les deux versions :
la fenêtre lisible perd EXACTEMENT UN GRAIN. Mesuré sur dl360-1, anneau de 9 grains :

    bobi-compute:0.45 (libmxl 1.1.0-rc1) → [9, 9, 9, 9, 9]
    bobi-compute:0.46 (libmxl 1.1.0)     → [8, 8, 8, 8, 8]

⚠ CE QUE CE BANC NE MONTRE PAS. Un banc qui lit la queue EN CONCURRENCE d'un écrivain rend ~99,6 %
de grains panachés sur les DEUX versions : avec un anneau de 9 grains et un écrivain qui en pose
un toutes les 0,5 ms, l'anneau tourne en 4,5 ms et le lecteur est en permanence dans la fenêtre
d'écriture. Le correctif déplace la frontière d'une case, **il ne rend pas la lecture en queue
sûre**. Un consommateur qui lit au ras de la queue perd simplement une case de marge — et il en
manquait déjà une.

⚠ L'ÉCRIVAIN DOIT ÊTRE À L'ARRÊT pendant la mesure. Sinon on mesure la course, pas la borne.

USAGE — conteneur JETABLE et domaine MXL PRIVÉ (un flux de test dans le domaine partagé est
visible de toute la flotte) :

    docker run --rm --entrypoint python3 --shm-size=1g \
      -e MXL_DOMAIN=/dev/shm/mxl-prof -e PYTHONPATH=/opt/script \
      -v /root/bench:/opt/script bobi-compute:<tag> \
      -c "import os;os.makedirs('/dev/shm/mxl-prof',exist_ok=True);\
          exec(open('/opt/script/mxl_fenetre_lisible.py').read())"
"""
import sys
import time

sys.path.insert(0, "/opt/script")
import numpy as np

import bobimxl

W, H, NOM = 320, 180, "test-fenetre-lisible"

inst = bobimxl.Instance()
w = bobimxl.Writer(inst, NOM, W, H, chroma="422", bit_depth=8, index_mode="free")
r = bobimxl.Reader(inst, NOM)

# Remplir l'anneau, PUIS cesser d'écrire.
for j in range(400):
    w.write(np.full((H * 3 // 2, W), j % 251, dtype=np.uint8), index=j)
time.sleep(0.3)

mesures = []
for _ in range(5):
    h = r.head_index()
    k = 0
    while k < 4096 and r.get(max(0, h - k - 1), timeout_ns=3_000_000) is not None:
        k += 1
    mesures.append(k)
    time.sleep(0.1)

print("  head = %d" % r.head_index())
print("  profondeurs mesurées : %s" % mesures)
if len(set(mesures)) != 1:
    print("  ⚠ INSTABLE — un écrivain tourne-t-il encore ? La mesure ne vaut rien.")
    sys.exit(2)
print("  fenêtre lisible : %d grains" % mesures[0])
