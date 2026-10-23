#!/bin/bash
# BANC : un flux TRANCHÉ franchit-il un lien RDMA sans retard ni déchirure ? (2026-10-12)
#
# Producteur de banc (prod.py, chaque tranche estampillée de son index de grain) sur le nœud SOURCE,
# réplication MANUELLE par mxl-fabrics-demo — mêmes arguments que services/rdma — vers le nœud
# CIBLE, puis lecture SIMULTANÉE des deux côtés pendant 20 s. Tout tourne dans une même image, de
# sorte que comparer deux versions de libmxl / de l'initiateur ne change qu'une variable.
#
#   rd.py     : lit CHAQUE index (get bloquant, comme l'initiateur) → écart index − estampille,
#               déchirure, instant de disponibilité (TAI local − instant de grille du grain).
#   rdhead.py : lit la TÊTE (head puis head-1, motif get_latest des consommateurs).
#
# usage : SRC=root@<ip agent> DST=root@<ip agent> SRC_FAB=<ip fabric src> DST_FAB=<ip fabric dst> \
#         [OFFSET_NS=<horloge dst − src>] [RDR=rd.py|rdhead.py] [ENVX="-e STALL_EVERY=5"] \
#         banc.sh NOM TRANCHES(30|0) RYTHME(fast|spread) PORT IMAGE
#   CPUSET : cœurs NON isolés où poser les conteneurs du banc (--cpuset-cpus). Sans lui, ils
#          peuvent tourner sur les cœurs isolés du moteur 2110 — l'alerte « placement CPU » le
#          signale, à juste titre (vu le 2026-10-12 sur dl360-1).
#   ENVX : -e MXL_SYNC_BATCH=2 (lot de synchro), -e STALL_EVERY=N (retard de 45 ms toutes les N
#          trames → 2 index sautés : reproduit le « contenu vieux d'un tour d'anneau »).
#
# Résultats de référence (2026-10-12, dl360-1 ↔ dell-1, bobi-compute:0.59 = libmxl 1.1.0) :
#   tranché / monolithique / lot 2 : réplique EXACTE, 0 déchirure, +3,7 / +3,7 / +0,4 ms.
#   STALL_EVERY=50 : 20 grains sur 1001 portent l'image d'il y a 10 grains, À LA SOURCE COMME À LA
#   RÉPLIQUE — muets en beta-1/rc1, marqués MXL_GRAIN_FLAG_INVALID en 1.1.0.
set -u
NAME=$1; NSL=$2; PACE=$3; PORT=$4; IMG=$5
: "${SRC:?}" "${DST:?}" "${SRC_FAB:?}" "${DST_FAB:?}"
OFF=${OFFSET_NS:-0}; RDR=${RDR:-rd.py}; ENVX=${ENVX:-}
ICI=$(cd "$(dirname "$0")" && pwd); RACINE=$(cd "$ICI/../../.." && pwd)
OUT=$(mktemp -d)
devs() {   # périphériques RDMA présents sur un nœud
    ssh "$1" 'echo --device /dev/infiniband/rdma_cm; for d in /dev/infiniband/uverbs*; do echo --device $d; done' | tr '\n' ' '
}
CAPS="--cap-add IPC_LOCK --ulimit memlock=-1:-1"
DEVS="$(devs "$SRC") $CAPS"; DEVT="$(devs "$DST") $CAPS"
PIN=${CPUSET:+--cpuset-cpus $CPUSET}
BENCH="-v /dev/shm:/dev/shm -v /root/slicebench:/bench --net=host $PIN"
for h in "$SRC" "$DST"; do
    ssh "$h" 'mkdir -p /root/slicebench'
    git -C "$RACINE" show HEAD:script_templates/bobimxl.py | ssh "$h" 'cat > /root/slicebench/bobimxl.py'
    scp -q "$ICI"/prod.py "$ICI"/rd.py "$ICI"/rdhead.py "$h":/root/slicebench/
done
ssh "$SRC" "docker rm -f sb-prod-$PORT sb-ini-$PORT >/dev/null 2>&1; docker run -d --name sb-prod-$PORT $BENCH $ENVX --entrypoint python3 $IMG /bench/prod.py $NAME $NSL $PACE 60 >/dev/null"
sleep 3
FID=$(ssh "$SRC" "docker run --rm $BENCH --entrypoint python3 $IMG -c 'import sys;sys.path.insert(0,\"/bench\");import bobimxl as b;print(b.flow_id(\"$NAME\"))'")
ssh "$SRC" "cat /dev/shm/mxl/$FID.mxl-flow/flow_def.json" | ssh "$DST" "cat > /dev/shm/sb-$PORT.flow.json"
ssh "$DST" "docker rm -f sb-tgt-$PORT >/dev/null 2>&1; rm -f /dev/shm/sb-$PORT.tinfo; docker run -d --name sb-tgt-$PORT -v /dev/shm:/dev/shm --net=host $PIN $DEVT --entrypoint mxl-fabrics-demo $IMG -d /dev/shm/mxl -f /dev/shm/sb-$PORT.flow.json --node $DST_FAB --service $PORT --provider verbs --target-info /dev/shm/sb-$PORT.tinfo >/dev/null"
for _ in $(seq 30); do ssh "$DST" "test -s /dev/shm/sb-$PORT.tinfo" && break; sleep 0.5; done
ssh "$DST" "cat /dev/shm/sb-$PORT.tinfo" | ssh "$SRC" "cat > /dev/shm/sb-$PORT.tinfo"
ssh "$SRC" "docker run -d --name sb-ini-$PORT -v /dev/shm:/dev/shm --net=host $PIN $DEVS --entrypoint mxl-fabrics-demo $IMG -i -d /dev/shm/mxl -f $FID --node $SRC_FAB --service $PORT --provider verbs --target-info @/dev/shm/sb-$PORT.tinfo >/dev/null"
sleep 6
R="docker run --rm $BENCH --entrypoint python3 $IMG /bench/$RDR $NAME 20"
[ "$RDR" = rd.py ] && ROFF=$OFF || ROFF=""
ssh "$SRC" "$R" > "$OUT/src.json" & ssh "$DST" "$R $ROFF" > "$OUT/rep.json" & wait
echo "--- source"; cat "$OUT/src.json"; echo "--- réplique"; cat "$OUT/rep.json"
ssh "$SRC" "docker rm -f sb-prod-$PORT sb-ini-$PORT >/dev/null; rm -rf /dev/shm/mxl/$FID.mxl-flow /dev/shm/sb-$PORT.tinfo"
ssh "$DST" "docker rm -f sb-tgt-$PORT >/dev/null; rm -rf /dev/shm/mxl/$FID.mxl-flow /dev/shm/sb-$PORT.tinfo /dev/shm/sb-$PORT.flow.json"
rm -rf "$OUT"
