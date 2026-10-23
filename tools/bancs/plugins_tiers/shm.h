// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// shm.h — contrat de la mémoire partagée moteur ↔ hôte co-processus (banc plugins tiers, étape 0).
//
// Protocole (un seul bloc en vol) :
//   moteur : si host_ready && done == req  → copie in[], req = s+1 (release), FUTEX_WAKE(req)
//            puis FUTEX_WAIT_BITSET(done) jusqu'à l'ÉCHÉANCE absolue ; accepté seulement si
//            out_seq == s+1 (une sortie d'un hôte précédent ou d'un bloc abandonné n'est jamais servie).
//   hôte   : FUTEX_WAIT(req) ; lit in[], traite, écrit out[], out_seq = req, done = req, FUTEX_WAKE(done).
//   Échéance dépassée → le bloc passe SEC ; tant que l'hôte n'a pas rendu (done != req), les blocs
//   suivants passent secs aussi (jamais deux blocs en vol : pas de course sur in[]).
#pragma once
#include <stdatomic.h>
#include <stdint.h>

#define SHM_MAXB 1024

struct shm {
    _Alignas(64) _Atomic uint32_t req;      // mot futex : soumission
    _Alignas(64) _Atomic uint32_t done;     // mot futex : bloc rendu
    _Atomic uint32_t out_seq;               // numéro du bloc dont out[] est la sortie
    _Atomic uint32_t host_ready;            // 1 = plugin instancié, prêt
    _Atomic int32_t host_pid;
    _Atomic int32_t latency;                // latence déclarée par le plugin (échantillons)
    _Atomic uint64_t host_proc_ns;          // durée du dernier pc_process dans l'hôte
    _Atomic uint64_t host_wake_ns;          // horodatage CLOCK_MONOTONIC du réveil de l'hôte (dernier bloc)
    _Atomic uint64_t host_ready_ns;         // horodatage de host_ready = 1
    _Atomic uint64_t host_init_ns;          // durée d'instanciation (lilv) de l'hôte courant
    int32_t n;                              // échantillons par bloc
    uint64_t base_ns, period_ns;            // grille d'horloge du moteur (mode async seulement)
    _Alignas(64) float in[2][SHM_MAXB];
    _Alignas(64) float out[2][SHM_MAXB];
};
