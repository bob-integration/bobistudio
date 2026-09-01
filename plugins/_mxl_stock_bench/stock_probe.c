/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France
 *
 * Sonde du BANC CROISÉ interop (MXL_INTEROP.md) : un consommateur vidéo écrit contre le SDK
 * MXL **STOCK** (headers + libmxl non patchés) — exactement ce que ferait un container d'un
 * AUTRE éditeur. Contrairement à `mxl-info` (qui ne lit que le descripteur en mémoire
 * partagée, et affiche donc N'IMPORTE quel flow), cette sonde emprunte le VRAI chemin de
 * lecture : mxlCreateFlowReader() — celui qui parse le flow_def et lève sur un media_type
 * inconnu (FlowParser.cpp) — puis mxlFlowReaderGetGrain().
 *
 * Attendu :
 *   - flow `video/v210`          (notre miroir)  → READER_OK + GRAIN_OK  (interop prouvée)
 *   - flow `video/x-mxl-planar`  (type maison)   → READER_FAIL           (rupture prouvée)
 *
 * Usage : stock_probe <domaine> <flowId>
 * Build : gcc -O2 stock_probe.c -o stock_probe -lmxl   (dans l'image bobi-mxl-stock)
 */
#include <stdio.h>
#include <stdint.h>
#include <mxl/mxl.h>
#include <mxl/flow.h>
#include <mxl/flowinfo.h>

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <domain> <flowId>\n", argv[0]);
        return 2;
    }
    const char *domain = argv[1], *flow_id = argv[2];

    mxlInstance inst = mxlCreateInstance(domain, NULL);
    if (!inst) {
        printf("INSTANCE_FAIL\n");
        return 1;
    }

    mxlFlowReader reader = NULL;
    mxlStatus st = mxlCreateFlowReader(inst, flow_id, NULL, &reader);
    if (st != MXL_STATUS_OK || !reader) {
        /* C'est ICI que le SDK stock rejette un media_type qu'il ne connaît pas. */
        printf("READER_FAIL status=%d\n", (int)st);
        mxlDestroyInstance(inst);
        return 1;
    }
    printf("READER_OK\n");

    mxlFlowInfo info;
    st = mxlFlowReaderGetInfo(reader, &info);
    if (st != MXL_STATUS_OK) {
        printf("INFO_FAIL status=%d\n", (int)st);
        mxlReleaseFlowReader(inst, reader);
        mxlDestroyInstance(inst);
        return 1;
    }
    /* AUDIO = flow CONTINU (samples), pas de grains → chemin mxlFlowReaderGetSamples.
       L'audit dit « audio float32 CONFORME byte-identique » : à vérifier ici. */
    if (info.config.common.format == MXL_DATA_FORMAT_AUDIO) {
        printf("FORMAT=AUDIO CHANNELS=%u BUFFER_LEN=%u HEAD_INDEX=%llu\n",
               info.config.continuous.channelCount,
               info.config.continuous.bufferLength,
               (unsigned long long)info.runtime.headIndex);
        mxlWrappedMultiBufferSlice slices;
        size_t count = 48;                       /* 1 ms @ 48 kHz */
        st = mxlFlowReaderGetSamples(reader, info.runtime.headIndex, count,
                                     200000000, &slices);
        if (st == MXL_STATUS_OK && slices.base.fragments[0].pointer) {
            const float *s = (const float *)slices.base.fragments[0].pointer;
            printf("SAMPLES_OK count=%zu stride=%zu frag0=%zu firstSamples=%.4f,%.4f,%.4f\n",
                   count, slices.stride, slices.base.fragments[0].size,
                   s[0], s[1], s[2]);
        } else {
            printf("SAMPLES_FAIL status=%d\n", (int)st);
        }
        mxlReleaseFlowReader(inst, reader);
        mxlDestroyInstance(inst);
        return 0;
    }

    printf("FORMAT=%s HEAD_INDEX=%llu GRAIN_COUNT=%u SLICE_SIZE=%u\n",
           info.config.common.format == MXL_DATA_FORMAT_DATA ? "DATA/ANC" : "VIDEO",
           (unsigned long long)info.runtime.headIndex,
           info.config.discrete.grainCount,
           info.config.discrete.sliceSizes[0]);

    /* Lecture d'un grain réel au head : preuve que la donnée est exploitable, pas juste le
       descripteur. `headIndex` peut désigner le grain EN COURS d'écriture → on retombe sur
       head-1 comme le font nos readers. */
    mxlGrainInfo gi;
    uint8_t *payload = NULL;
    uint64_t head = info.runtime.headIndex;
    st = mxlFlowReaderGetGrain(reader, head, 200000000, &gi, &payload);
    if (st != MXL_STATUS_OK && head > 0)
        st = mxlFlowReaderGetGrain(reader, head - 1, 200000000, &gi, &payload);
    if (st == MXL_STATUS_OK && payload) {
        printf("GRAIN_OK size=%u totalSlices=%u validSlices=%u firstBytes=%02x%02x%02x%02x\n",
               gi.grainSize, (unsigned)gi.totalSlices, (unsigned)gi.validSlices,
               payload[0], payload[1], payload[2], payload[3]);
    } else {
        printf("GRAIN_FAIL status=%d\n", (int)st);
    }

    mxlReleaseFlowReader(inst, reader);
    mxlDestroyInstance(inst);
    return 0;
}
