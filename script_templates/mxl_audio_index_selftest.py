# SPDX-License-Identifier: GPL-3.0-or-later
"""Aller-retour d'index audio MXL : ce qu'on ÉCRIT à l'index k se relit-il à l'index k ?

L'API C prend un index ONE-PAST-THE-END ([index - count, index)) alors que notre binding expose
un index de DÉBUT. La conversion se fait à la frontière ctypes ; ce test la prouve sur le vrai
libmxl, plutôt que de la déduire de la documentation.

Chaque bloc porte une valeur constante distincte (bloc j → j+1) : si un décalage d'un bloc
subsiste, la valeur relue ne sera pas celle attendue et le test le NOMME, au lieu de rendre un
« ça a l'air bon » sur des données indiscernables.

QUAND LE RELANCER : à toute montée du SDK MXL, et dès qu'un plugin écrit ou lit l'audio par
blocs d'une autre taille que nos 48 samples. C'est là que le défaut redevient audible — la
confusion d'index ne s'entend pas tant que producteur et consommateur partagent la même taille
de bloc (ils se trompent alors ENSEMBLE, cf. le contrôle contre l'ancien binding : blocs 1 à 4
relus « correctement », seuls head_index et le tout premier bloc trahissaient l'erreur).

USAGE — conteneur JETABLE et domaine MXL PRIVÉ, jamais le bus de production (créer un flux dans
le domaine partagé le rend visible de toute la flotte, et un flux de test n'a rien à y faire) :

    docker run --rm --entrypoint python3 --shm-size=512m \
      -e MXL_DOMAIN=/dev/shm/mxl-idxtest \
      -v /root/bench:/opt/script bobi-compute:0.24 \
      -c "import os;os.makedirs('/dev/shm/mxl-idxtest',exist_ok=True);\
          exec(open('/opt/script/mxl_audio_index_selftest.py').read())"

Sortie 0 = convention respectée, 1 = décalage, et le message dit lequel.
"""
import sys

sys.path.insert(0, "/opt/script")
import numpy as np

import bobimxl

N = 48                      # notre bloc partout : 1 ms à 48 kHz
CH = 8
NB = 5
NOM = "test-idx-roundtrip"

inst = bobimxl.Instance()
w = bobimxl.AudioWriter(inst, NOM, channels=CH, sample_rate=48000, index_mode="counter")
r = bobimxl.AudioReader(inst, NOM)

echecs = []

# Écriture de NB blocs à des index de DÉBUT connus : 0, 48, 96, …
debuts = []
for j in range(NB):
    bloc = np.full((N, CH), float(j + 1), dtype=np.float32)
    debuts.append(w.write(bloc, index=j * N))

print("index de début rendus par write() :", debuts)
if debuts != [j * N for j in range(NB)]:
    echecs.append("write() ne rend pas l'index de DÉBUT demandé")

print("head_index après écriture :", r.head_index(), "(attendu %d = one-past-the-end)" % (NB * N))
if r.head_index() != NB * N:
    echecs.append("head_index = %s, attendu %d — l'index n'est pas one-past-the-end"
                  % (r.head_index(), NB * N))

# RELECTURE à l'index de début : le bloc j doit valoir j+1, partout.
for j in range(NB):
    blk = r.read_from(j * N, N)
    if blk is None:
        echecs.append("read_from(%d, %d) → None" % (j * N, N))
        continue
    vus = sorted(set(blk.reshape(-1).tolist()))
    if vus != [float(j + 1)]:
        echecs.append("read_from(%d) rend %s, attendu [%.1f] → décalage de %s bloc(s)"
                      % (j * N, vus, j + 1, (vus[0] - (j + 1)) if len(vus) == 1 else "?"))
    else:
        print("  read_from(%4d, %d) → %.1f  ✓" % (j * N, N, vus[0]))

# read_latest doit rendre le DERNIER bloc écrit (valeur NB), pas celui d'avant.
last = r.read_latest(N)
if last is None:
    echecs.append("read_latest → None")
else:
    vus = sorted(set(last.reshape(-1).tolist()))
    print("  read_latest(%d)      → %s  (attendu [%.1f])" % (N, vus, float(NB)))
    if vus != [float(NB)]:
        echecs.append("read_latest rend %s, attendu [%.1f]" % (vus, float(NB)))

print()
if echecs:
    print("ÉCHEC :")
    for e in echecs:
        print("  -", e)
    sys.exit(1)
print("OK — écriture et relecture partagent la même convention d'index de début.")
