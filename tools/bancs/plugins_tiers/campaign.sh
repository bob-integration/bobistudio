#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# campaign.sh — campagne complète du banc plugins tiers, étape 0 (≈ 1 h 45), DANS le conteneur jetable.
# Cœurs : $1 = moteur, $2 = hôte « autre cœur », $3 = cœur des sondes (défauts 81 83 85 : dell-1, NUMA 1,
# hors de tout cœur isolé). Résultats : results/ (SCHED_FIFO), results_sched_other/ (sans temps réel).
E=${1:-81}; H=${2:-83}; S=${3:-85}
cd "$(dirname "$0")"
python3 run_bench.py run 'fault-*' async-other-dpl --cpus $E,$H --fifo 70 --out results
python3 run_bench.py run 'inproc-*' 'coproc-same-*' 'coproc-other-*' --minutes 3 --cpus $E,$H --fifo 70 --out results
python3 run_bench.py run inproc-null inproc-dpl inproc-lspmb coproc-same-null coproc-same-dpl coproc-same-lspmb \
    coproc-other-null coproc-other-dpl coproc-other-lspmb --minutes 3 --cpus $E,$H --fifo 0 --out results_sched_other
python3 run_bench.py probe --out results --cpu $S
echo CAMPAIGN_DONE
