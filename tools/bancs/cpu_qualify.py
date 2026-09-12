#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# cpu_qualify.py — QUALIFICATION du CPU d'un nœud (chantier « quota par modèle de CPU »,
# cf. réglage mtl_sch_quota_mbs).
#
# Pourquoi : le quota d'un scheduler libmtl (Mb/s de trafic ST 2110 qu'un lcore busy-poll peut
# parser + recopier) est aujourd'hui un réglage GLOBAL (`mtl_sch_quota_mbs`, défaut 2500) alors
# que c'est une propriété PHYSIQUE du CPU qui exécute le lcore. L'objectif final est une
# bibliothèque de profils par modèle de CPU, sur le modèle de `nic_profiles` (cf. app/nic_qualify.py).
# Ce script est la SONDE : il relève l'identité du CPU d'une machine et fait tourner un micro-banc
# mono-cœur, puis IMPRIME UN JSON SUR STDOUT. Il n'écrit rien en base, n'ouvre aucune connexion
# réseau, et n'importe pas le paquet `app` — il est fait pour tourner isolément sur un nœud distant
# via host-exec (agent-nœud), où seule la bibliothèque standard est garantie.
#
# ⚠⚠ DISCIPLINE — LIRE AVANT DE TOUCHER À `quota_mbs_estime` ⚠⚠
# Ce script NE MESURE PAS le quota d'un scheduler libmtl. Le seul moyen de mesurer un quota
# AUTORITAIRE est de charger un scheduler réel avec du trafic ST 2110 jusqu'au décrochage
# (perte de trames / cinst hors bornes) — cf. la discipline équivalente sur les cartes réseau
# dans app/nic_qualify.py (`measured_tx_cap` : pas de preuve de clamp ⇒ pas de cap écrit). Un banc
# mono-cœur qui recopie de la mémoire et parse des en-têtes RTP synthétiques ne fait tourner NI
# libmtl NI DPDK NI de vrai trafic réseau : c'est un PROXY, pas une mesure du plafond. Confondre les
# deux — mesurer une DEMANDE ou une capacité brute en croyant mesurer un PLAFOND — est exactement le
# bug qui a coûté une panne de production le jour où ce fichier a été écrit (cf. l'historique de
# `measured_tx_cap` dans app/nic_qualify.py). En conséquence :
#   - `quota_mbs_estime` porte TOUJOURS `"indicatif": true` et `"ancrage": null` — il n'y a et il
#     n'y aura jamais de mesure qui « ancre » ce nombre tant qu'aucun scheduler réel n'a été chargé.
#   - `avertissements` DOIT toujours contenir le rappel que la valeur est indicative.
#   - Ne JAMAIS renommer ce champ en quelque chose qui sonnerait comme une capacité prouvée
#     (« quota_mbs », « rl_quota », « cap_mbs », …) : le nom `_estime` est la garde, pas un détail.
#
# Usage :
#   ./venv/bin/python tools/cpu_qualify.py            # JSON complet sur stdout (défaut)
#   ./venv/bin/python tools/cpu_qualify.py --court     # résumé lisible sur stdout
#
# Code de sortie toujours 0 (sauf erreur Python non rattrapée) : une sonde qui échoue pose un champ
# manquant + un avertissement plutôt que de faire planter tout le relevé — un host-exec distant qui
# plante sur une machine bizarre est plus coûteux à diagnostiquer qu'un JSON incomplet mais honnête.

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time

# ─── Identité CPU ─────────────────────────────────────────────────────────────────────────────────

def _read_proc_cpuinfo():
    """Contenu brut de /proc/cpuinfo, ou '' si illisible (conteneur très restreint, non-Linux)."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            return f.read()
    except OSError:
        return ""


def _parse_cpuinfo(text, avertissements):
    """Extrait les champs utiles du PREMIER bloc processeur de /proc/cpuinfo (identité — les
    threads logiques partagent le même modèle) + le nombre de blocs (= threads logiques vus par
    le noyau). Ne dépend d'aucun outil externe : c'est le repli minimal garanti sur tout Linux."""
    out = {}
    if not text:
        avertissements.append("/proc/cpuinfo illisible ou vide — identité CPU incomplète")
        return out
    blocs = text.split("\n\n")
    blocs = [b for b in blocs if b.strip()]
    out["threads_logiques_proc"] = len(blocs)
    premier = blocs[0] if blocs else ""

    def champ(nom, cast=str):
        m = re.search(r"^%s\s*:\s*(.*)$" % re.escape(nom), premier, re.M)
        if not m:
            return None
        v = m.group(1).strip()
        try:
            return cast(v)
        except (TypeError, ValueError):
            return v

    out["model_name"] = champ("model name") or ""
    out["vendor_id"] = champ("vendor_id") or ""
    out["famille"] = champ("cpu family", int)
    out["modele"] = champ("model", int)
    out["stepping"] = champ("stepping", int)
    flags = champ("flags") or champ("Features") or ""
    out["_flags_brutes"] = flags
    return out


def _run(cmd, timeout=8):
    """Exécute `cmd` (liste d'arguments), renvoie stdout ou '' si l'outil est absent / échoue.
    Locale forcée en C : `lscpu` traduit ses libellés de champ selon la locale (vu en français sur
    ce dépôt), ce qui casserait le parsing par nom de champ anglais."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_lscpu_json(text):
    """`lscpu -J` (locale C) → dict {libellé_de_champ_sans_':' : valeur brute}. '' si absent/invalide."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    out = {}
    for row in (data.get("lscpu") or []):
        champ = (row.get("field") or "").rstrip(":").strip()
        if champ:
            out[champ] = row.get("data")
        for enfant in (row.get("children") or []):
            c2 = (enfant.get("field") or "").rstrip(":").strip()
            if c2:
                out[c2] = enfant.get("data")
    return out


def _to_int(v):
    if v is None:
        return None
    m = re.search(r"-?\d+", str(v))
    return int(m.group(0)) if m else None


def _to_float(v):
    if v is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m else None


def _cache_bytes(lscpu_bytes, libelle):
    """Taille de cache en octets depuis `lscpu --bytes` (le champ porte déjà les octets bruts sans
    unité, ex. '65536 (2 instances)') — plus fiable que de reparser '64 KiB'."""
    v = lscpu_bytes.get(libelle)
    return _to_int(v)


def identite_cpu(avertissements):
    """Identité CPU consolidée : /proc/cpuinfo (toujours dispo sur Linux) enrichi par `lscpu` si
    présent (sockets/NUMA/caches/fréquences que /proc/cpuinfo seul ne donne pas proprement)."""
    info = _parse_cpuinfo(_read_proc_cpuinfo(), avertissements)
    flags = set((info.pop("_flags_brutes", "") or "").split())

    lscpu_txt = _run(["lscpu", "-J"])
    lscpu = _parse_lscpu_json(lscpu_txt)
    lscpu_bytes_txt = _run(["lscpu", "--bytes"])
    # Reparse la sortie --bytes en dict {champ: valeur} texte simple (pas de -J --bytes portable
    # sur toutes les versions d'util-linux → deux appels, l'un pour la structure, l'autre pour les octets).
    lscpu_bytes = {}
    for ligne in (lscpu_bytes_txt or "").splitlines():
        if ":" in ligne:
            k, _, v = ligne.partition(":")
            lscpu_bytes[k.strip()] = v.strip()

    if not lscpu:
        avertissements.append("lscpu indisponible — sockets/NUMA/caches/fréquences non relevés "
                               "(repli /proc/cpuinfo seul)")

    if not info.get("model_name"):
        info["model_name"] = lscpu.get("Model name") or ""
    if not info.get("vendor_id"):
        info["vendor_id"] = lscpu.get("Vendor ID") or ""
    info["famille"] = info.get("famille") if info.get("famille") is not None else _to_int(lscpu.get("CPU family"))
    info["modele"] = info.get("modele") if info.get("modele") is not None else _to_int(lscpu.get("Model"))
    info["stepping"] = info.get("stepping") if info.get("stepping") is not None else _to_int(lscpu.get("Stepping"))

    threads_logiques = _to_int(lscpu.get("CPU(s)")) or info.get("threads_logiques_proc") or os.cpu_count()
    sockets = _to_int(lscpu.get("Socket(s)"))
    coeurs_par_socket = _to_int(lscpu.get("Core(s) per socket"))
    threads_par_coeur = _to_int(lscpu.get("Thread(s) per core"))
    coeurs_physiques = None
    if sockets and coeurs_par_socket:
        coeurs_physiques = sockets * coeurs_par_socket
    elif threads_logiques and threads_par_coeur:
        coeurs_physiques = threads_logiques // max(1, threads_par_coeur)
    else:
        avertissements.append("nombre de cœurs physiques non déterminé avec certitude "
                               "(lscpu absent/incomplet) — repli sur le nombre de threads logiques")
        coeurs_physiques = threads_logiques

    freq_base = _to_float(lscpu.get("CPU MHz"))
    freq_max = _to_float(lscpu.get("CPU max MHz"))
    if freq_max is None and freq_base is not None:
        freq_max = freq_base   # pas de scaling exposé (repli honnête, pas une estimation cachée)

    return {
        "model_name": info.get("model_name") or "",
        "vendor_id": info.get("vendor_id") or "",
        "famille": info.get("famille"),
        "modele": info.get("modele"),
        "stepping": info.get("stepping"),
        "sockets": sockets,
        "coeurs_physiques": coeurs_physiques,
        "threads_logiques": threads_logiques,
        "noeuds_numa": _to_int(lscpu.get("NUMA node(s)")),
        "freq_base_mhz": freq_base,
        "freq_max_mhz": freq_max,
        "cache_l1d_octets": _cache_bytes(lscpu_bytes, "L1d cache"),
        "cache_l2_octets": _cache_bytes(lscpu_bytes, "L2 cache"),
        "cache_l3_octets": _cache_bytes(lscpu_bytes, "L3 cache"),
        "avx2": "avx2" in flags,
        "avx512f": "avx512f" in flags,
        "sse4_2": "sse4_2" in flags,
    }


# ─── Micro-banc mono-cœur ─────────────────────────────────────────────────────────────────────────

def _epingler_coeur_courant(cible_imposee=None):
    """Épingle le process (donc ce thread) sur UN SEUL cœur logique, pour que le banc ne migre pas
    en cours de mesure. Best-effort : `sched_setaffinity` n'existe que sous Linux et peut être refusé
    (conteneur restreint) — dans ce cas on continue non épinglé et on le dit dans les avertissements."""
    if not hasattr(os, "sched_setaffinity"):
        return None, "os.sched_setaffinity indisponible (non-Linux ?)"
    try:
        dispo = sorted(os.sched_getaffinity(0))
        if not dispo:
            return None, "aucun cœur disponible via sched_getaffinity"
        # ★ Cœur IMPOSÉ par l'appelant quand il en sait plus que nous : sur un nœud qui fait tourner
        # le moteur 2110, les premiers cœurs logiques sont en busy-poll DPDK. S'y épingler mesure la
        # CONTENTION, pas le processeur (constaté : 3,6 Go/s au lieu du régime réel sur un Xeon Gold).
        # L'appelant (app/cpu_qualify.py) choisit un cœur hors de l'empreinte moteur.
        cible = cible_imposee if (cible_imposee in dispo) else dispo[0]
        os.sched_setaffinity(0, {cible})
        return cible, ("" if cible_imposee is None or cible == cible_imposee
                       else "cœur %s demandé mais indisponible — replié sur %s" % (cible_imposee, cible))
    except OSError as e:
        return None, "sched_setaffinity refusé (%s)" % e


def bench_memcpy_gbps(avertissements, duree_cible_s=2.5):
    """Débit memcpy soutenu, mono-thread, sur un tampon PLUS GRAND que L3 (coût par octet dominant :
    on veut le régime bande-passante-mémoire, pas le régime cache). Repose sur `ctypes.memmove` —
    même primitive que le canary bande-passante mémoire (app/membw.py), aucune dépendance externe.
    Renvoie (gbps, octets_par_transfert, transferts) ou (None, ...) si la sonde échoue."""
    taille = 128 * 1024 * 1024  # 128 Mio : au-delà de tout L3 réaliste (vu ici : 32 Mio)
    try:
        src = ctypes.create_string_buffer(taille)
        dst = ctypes.create_string_buffer(taille)
    except MemoryError:
        avertissements.append("memcpy : allocation de 128 Mio échouée (mémoire contrainte) — "
                               "sonde memcpy_gbps non renseignée")
        return None, taille, 0
    mm = ctypes.memmove
    mm(dst, src, taille)   # tour de chauffe (pages touchées, pas dans le chrono)
    transferts = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duree_cible_s:
        mm(dst, src, taille)
        transferts += 1
    dt = time.perf_counter() - t0
    if transferts == 0 or dt <= 0:
        avertissements.append("memcpy : durée de mesure nulle — sonde memcpy_gbps non renseignée")
        return None, taille, 0
    gbps = (transferts * taille) / dt / 1e9
    return round(gbps, 3), taille, transferts


def bench_pkt_parse_mpps(avertissements, duree_cible_s=2.5):
    """Millions de paquets/s d'une boucle qui lit un en-tête RTP (12 octets) et recopie une charge
    utile de 1440 octets — taille représentative d'un paquet ST 2110 (MTU jumbo, payload vidéo).
    Boucle Python pure (aucune bibliothèque C dispo à coup sûr côté agent-nœud) : ce n'est donc PAS
    une mesure de ce que ferait libmtl en C/DPDK, seulement un proxy de la vitesse « parsing +
    recopie mémoire » relative d'un cœur à l'autre — cf. garde en tête de module."""
    entete_rtp = bytes([0x80, 0x60] + [0] * 10)   # V=2,P=0,X=0,CC=0 ; PT=0x60 ; seq/ts/ssrc à 0
    charge_utile = bytes(1440)
    paquet = entete_rtp + charge_utile

    def _lire_entete(pkt):
        # Champs RTP usuels décodés (comme le ferait un parseur ST 2110 minimal) : version/padding/
        # extension/CC, marker/payload-type, séquence, timestamp, SSRC.
        b0, b1 = pkt[0], pkt[1]
        version = b0 >> 6
        marker = (b1 >> 7) & 1
        pt = b1 & 0x7F
        seq = (pkt[2] << 8) | pkt[3]
        ts = int.from_bytes(pkt[4:8], "big")
        ssrc = int.from_bytes(pkt[8:12], "big")
        return version, marker, pt, seq, ts, ssrc

    compte = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duree_cible_s:
        for _ in range(2000):   # lot entre deux appels d'horloge (coût de perf_counter() non négligeable)
            _lire_entete(paquet)
            _ = paquet[12:]     # recopie de la charge utile (bytes → nouvelle allocation, comme un vrai parseur)
            compte += 1
    dt = time.perf_counter() - t0
    if compte == 0 or dt <= 0:
        avertissements.append("pkt_parse : durée de mesure nulle — sonde pkt_parse_mpps non renseignée")
        return None, len(paquet)
    mpps = compte / dt / 1e6
    return round(mpps, 4), len(paquet)


def executer_banc(avertissements, coeur_cible=None):
    """Lance le micro-banc complet (mémoire + parsing), borné à ~5 s au total, épinglé sur un cœur
    si possible. Renvoie le dict `bench`."""
    t_debut = time.perf_counter()
    coeur, note_affinite = _epingler_coeur_courant(coeur_cible)
    if note_affinite:
        avertissements.append("épinglage CPU : %s — le banc peut migrer entre cœurs pendant la "
                               "mesure (résultat moins stable)" % note_affinite)

    memcpy_gbps, taille_buf, transferts = bench_memcpy_gbps(avertissements)
    pkt_mpps, taille_paquet = bench_pkt_parse_mpps(avertissements)

    duree_totale = round(time.perf_counter() - t_debut, 3)
    return {
        "coeur_epingle": coeur,
        "memcpy_gbps": memcpy_gbps,
        "memcpy_taille_buffer_octets": taille_buf,
        "memcpy_transferts": transferts,
        "pkt_parse_mpps": pkt_mpps,
        "pkt_parse_taille_paquet_octets": taille_paquet,
        "duree_totale_s": duree_totale,
    }


# ─── Estimation indicative du quota (JAMAIS une mesure — cf. en-tête) ──────────────────────────────

def estimer_quota(bench, avertissements):
    """Dérive un `quota_mbs_estime` INDICATIF depuis le proxy du banc. Formule volontairement simple
    et EXPLICITÉE en clair dans `methode` (pas de coefficient caché) :

      1. plafond_copie_mbs   = memcpy_gbps × 8000                      (Gio/s → Mb/s, débit copie pur)
      2. plafond_parsing_mbs = pkt_parse_mpps × taille_paquet_octets × 8
                               (paquets/s du proxy → Mb/s si chaque paquet portait ce débit)
      3. plafond_brut_mbs    = min(plafond_copie_mbs, plafond_parsing_mbs)
                               (un scheduler doit FAIRE LES DEUX par paquet — le plus lent domine)
      4. quota_mbs_estime    = plafond_brut_mbs × FACTEUR_PRUDENCE (0.35)
                               (le banc mesure un cœur ISOLÉ ne faisant QUE ça, en boucle Python pure
                               pour le parsing — un scheduler réel partage le cœur avec l'ordonnanceur
                               DPDK, la pile réseau AF-XDP/DPDK, et tourne en C, pas en Python ; le
                               facteur ramène le proxy à un ordre de grandeur PLUS PROCHE d'un usage
                               réel, sans prétendre le reproduire)

    Ce nombre ne doit JAMAIS remplacer une mesure de plafond obtenue en chargeant un scheduler réel
    jusqu'au décrochage — cf. l'en-tête du module."""
    FACTEUR_PRUDENCE = 0.35
    memcpy_gbps = bench.get("memcpy_gbps")
    pkt_mpps = bench.get("pkt_parse_mpps")
    taille_paquet = bench.get("pkt_parse_taille_paquet_octets") or 1452

    if memcpy_gbps is None or pkt_mpps is None:
        avertissements.append("quota_mbs_estime non calculé : une des deux sondes du banc a échoué")
        return {
            "valeur": None,
            "indicatif": True,
            "ancrage": None,
            "methode": "non calculable — sonde(s) manquante(s), cf. avertissements",
        }

    plafond_copie_mbs = memcpy_gbps * 8000.0
    plafond_parsing_mbs = pkt_mpps * taille_paquet * 8.0
    plafond_brut_mbs = min(plafond_copie_mbs, plafond_parsing_mbs)
    valeur = round(plafond_brut_mbs * FACTEUR_PRUDENCE, 1)

    return {
        "valeur": valeur,
        "indicatif": True,
        "ancrage": None,
        "methode": (
            "min(memcpy_gbps×8000, pkt_parse_mpps×%d×8) × facteur de prudence %.2f — "
            "PROXY mono-cœur (memcpy + boucle Python), PAS une mesure de scheduler libmtl réel "
            "chargé jusqu'au décrochage. plafond_copie=%.0f Mb/s, plafond_parsing=%.0f Mb/s."
            % (taille_paquet, FACTEUR_PRUDENCE, plafond_copie_mbs, plafond_parsing_mbs)
        ),
    }


# ─── Avertissements transverses ────────────────────────────────────────────────────────────────────

def verifier_charge_machine(avertissements):
    """Avertit si la machine est visiblement chargée pendant la mesure (/proc/loadavg) : un banc
    mono-cœur exécuté sous contention CPU externe donne un débit/pkt-rate ARTIFICIELLEMENT bas (le
    scheduler du noyau préempte notre boucle), ce qui fausserait `quota_mbs_estime` à la baisse."""
    try:
        with open("/proc/loadavg", "r") as f:
            champs = f.read().split()
        load1 = float(champs[0])
    except (OSError, ValueError, IndexError):
        avertissements.append("charge machine non vérifiable (/proc/loadavg illisible)")
        return
    nb_cpu = os.cpu_count() or 1
    if load1 > 0.5 * nb_cpu:
        avertissements.append(
            "machine visiblement chargée pendant la mesure (load1=%.2f sur %d CPU) — le banc "
            "mono-cœur peut être ralenti par contention, ce qui SOUS-ESTIME memcpy_gbps/"
            "pkt_parse_mpps et donc quota_mbs_estime : refaire la mesure au calme si possible"
            % (load1, nb_cpu))


# ─── Point d'entrée ────────────────────────────────────────────────────────────────────────────────

def relever(avertissements, coeur_cible=None):
    cpu = identite_cpu(avertissements)
    verifier_charge_machine(avertissements)
    bench = executer_banc(avertissements, coeur_cible)
    quota = estimer_quota(bench, avertissements)
    avertissements.append(
        "quota_mbs_estime est INDICATIF (ancrage=null) : dérivé d'un micro-banc mono-cœur "
        "(memcpy + parsing RTP synthétique en boucle Python), pas d'un scheduler libmtl réel "
        "chargé jusqu'au décrochage — ne PAS l'écrire comme quota autoritaire sans qualification "
        "réelle (charge de trafic ST 2110 jusqu'à perte de trames), cf. app/nic_qualify.py pour la "
        "discipline équivalente sur les cartes réseau.")
    return {
        "cpu": cpu,
        "bench": bench,
        "quota_mbs_estime": quota,
        "avertissements": avertissements,
        "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _resume_court(resultat):
    cpu = resultat["cpu"]
    bench = resultat["bench"]
    quota = resultat["quota_mbs_estime"]
    lignes = [
        "CPU : %s" % (cpu.get("model_name") or "?"),
        "  vendeur=%s famille=%s modele=%s stepping=%s" % (
            cpu.get("vendor_id"), cpu.get("famille"), cpu.get("modele"), cpu.get("stepping")),
        "  sockets=%s coeurs_physiques=%s threads_logiques=%s numa=%s" % (
            cpu.get("sockets"), cpu.get("coeurs_physiques"), cpu.get("threads_logiques"),
            cpu.get("noeuds_numa")),
        "  freq base/max = %s/%s MHz" % (cpu.get("freq_base_mhz"), cpu.get("freq_max_mhz")),
        "  cache L1d/L2/L3 = %s/%s/%s octets" % (
            cpu.get("cache_l1d_octets"), cpu.get("cache_l2_octets"), cpu.get("cache_l3_octets")),
        "  avx2=%s avx512f=%s sse4_2=%s" % (cpu.get("avx2"), cpu.get("avx512f"), cpu.get("sse4_2")),
        "",
        "Banc (%.2fs, cœur épinglé=%s) :" % (bench.get("duree_totale_s") or 0, bench.get("coeur_epingle")),
        "  memcpy_gbps    = %s" % bench.get("memcpy_gbps"),
        "  pkt_parse_mpps = %s" % bench.get("pkt_parse_mpps"),
        "",
        "quota_mbs_estime = %s (indicatif=%s, ancrage=%s)" % (
            quota.get("valeur"), quota.get("indicatif"), quota.get("ancrage")),
        "  methode : %s" % quota.get("methode"),
        "",
        "Avertissements :",
    ]
    for a in resultat["avertissements"]:
        lignes.append("  - %s" % a)
    return "\n".join(lignes)


def main():
    p = argparse.ArgumentParser(
        description="Qualification CPU d'un nœud : identité + micro-banc mono-cœur (PROXY indicatif, "
                    "cf. en-tête du fichier — ne mesure PAS un quota libmtl autoritaire).")
    p.add_argument("--json", action="store_true", help="sortie JSON complète (défaut)")
    p.add_argument("--court", action="store_true", help="résumé lisible au lieu du JSON")
    p.add_argument("--coeur", type=int, default=None,
                   help="cœur logique sur lequel épingler le banc. À imposer quand la machine fait "
                        "tourner un moteur 2110 : les premiers cœurs sont en busy-poll DPDK et y "
                        "mesurer donne la contention, pas le processeur.")
    args = p.parse_args()

    avertissements = []
    try:
        resultat = relever(avertissements, args.coeur)
    except Exception as e:  # noqa: BLE001 — on ne veut JAMAIS planter côté host-exec distant
        resultat = {
            "cpu": {}, "bench": {}, "quota_mbs_estime": {
                "valeur": None, "indicatif": True, "ancrage": None,
                "methode": "non calculé (exception pendant le relevé)"},
            "avertissements": avertissements + ["relevé interrompu par une exception : %r" % e],
            "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    if args.court and not args.json:
        print(_resume_court(resultat))
    else:
        print(json.dumps(resultat, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
