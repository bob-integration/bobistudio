# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Qualification CPU d'un nœud — pendant de `app/nic_qualify.py` pour les processeurs.

POURQUOI. Le quota d'un scheduler libmtl (combien de Mb/s de ST 2110 un cœur en busy-poll parse et
recopie) est une propriété PHYSIQUE de la machine — fréquence, IPC, cache, bande passante mémoire.
Il vivait pourtant dans un réglage GLOBAL (`mtl_sch_quota_mbs`) appliqué à toute la flotte : sur un
nœud plus rapide c'est pessimiste et on gaspille des cœurs ; sur un nœud plus lent c'est optimiste et
on sous-dimensionne le moteur — ce qui ne se voit pas tout de suite, mais se paie en sessions
refusées ou en wedges sous jitter. D'où `cpu_profiles`, bibliothèque par modèle de CPU, et la cascade
`docker_driver.sch_quota_mbs` (nœud → profil → réglage global → plancher).

★ CE MODULE N'ÉCRIT JAMAIS DE QUOTA. Il relève l'IDENTITÉ du CPU et un PROXY (micro-banc mono-cœur :
memcpy + parsing d'en-têtes). Le proxy n'est PAS le quota : le vrai plafond ne s'obtient qu'en
chargeant un scheduler avec du trafic 2110 réel jusqu'au décrochage. Écrire une valeur non prouvée
serait rejouer exactement la panne du 2026-07-27 sur les cartes réseau, où une sonde relisait sa
propre demande en croyant mesurer un plafond et a fait dériver le cap de 63 à 14, en silence.
`measured` reste donc à 0 et `quota_mbs` n'est pas touché (l'upsert ignore les champs None) : tant
qu'aucune campagne de charge n'a ancré le proxy, la cascade retombe sur le repli prudent — et le dit.
"""

import base64
import json
import logging
import os

log = logging.getLogger(__name__)

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tools", "cpu_qualify.py")


def _coeur_hors_moteur(node):
    """Cœur logique HORS de l'empreinte du moteur 2110, sur lequel épingler le banc — ou None.

    Sur un nœud qui fait tourner un moteur, les premiers cœurs sont en busy-poll DPDK 100 % du temps.
    Y épingler le banc mesure la CONTENTION, pas le processeur : constaté sur dl360-1, 3,6 Go/s de
    memcpy sur un Xeon Gold 6240R, soit une fraction du régime réel. `core_pool.engine_cpu_footprint`
    est LA source de vérité de cette empreinte (et elle est HT-aware : les jumeaux des lcores en font
    partie), donc tout cœur hors d'elle est réellement libre."""
    try:
        from . import core_pool
        n_cpus = None
        try:
            from . import node_driver
            rc, out, _ = node_driver.host_exec(node, "nproc", timeout=15)
            n_cpus = int((out or "").strip()) if rc == 0 else None
        except Exception:
            pass
        if not n_cpus:
            return None
        moteur, _ht = core_pool.engine_cpu_footprint(n_cpus=n_cpus,
                                                     core_of=core_pool.read_cpu_core_map(node))
        libres = [c for c in range(n_cpus) if c not in moteur]
        return libres[-1] if libres else None      # le plus haut : le plus loin du housekeeping
    except Exception as e:
        log.debug("_coeur_hors_moteur: %s", e)
        return None


def _commande_sonde(coeur=None):
    """Commande shell qui exécute la sonde SUR LE NŒUD sans y déposer de fichier : le script est
    encodé en base64 et poussé dans l'entrée standard de `python3`. Évite un dépôt à nettoyer, et
    garantit que la sonde exécutée est bien celle de CETTE version de l'orchestrateur."""
    with open(_SCRIPT, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    opt = (" --coeur %d" % int(coeur)) if coeur is not None else ""
    return "echo %s | base64 -d | python3 - --json%s" % (b64, opt)


def _extraire_json(sortie):
    """Isole l'objet JSON de la sortie de la sonde (un shell distant peut préfixer du bruit)."""
    s = (sortie or "").strip()
    i = s.find("{")
    if i < 0:
        return None
    try:
        return json.loads(s[i:])
    except Exception:
        return None


def qualify_node_cpu(node, run=None):
    """Relève l'identité CPU + le micro-banc de `node`, persiste ce qui est SÛR, retourne le rapport.

    `run(cmd) -> str` : exécuteur (défaut = host-exec via l'agent-nœud). Écrit :
      · `nodes.cpu_model` — la clé qui relie ce nœud à la bibliothèque ;
      · `cpu_profiles` — identité + proxy, `measured=0`, **sans `quota_mbs`**.

    Retourne un dict `{ok, cpu_model, profil, estimation, avertissements}`, ou `{ok: False, error}`.
    """
    from .database import db_update_node, db_upsert_cpu_profile, db_get_cpu_profile

    if run is None:
        from . import node_driver

        def run(cmd):
            rc, out, _err = node_driver.host_exec(node, cmd, timeout=90)
            return out if rc == 0 else ""

    _coeur = _coeur_hors_moteur(node)
    try:
        brut = run(_commande_sonde(_coeur))
    except Exception as e:
        return {"ok": False, "error": "sonde CPU injoignable : %s" % e}
    rap = _extraire_json(brut)
    if not rap:
        return {"ok": False, "error": "sonde CPU : sortie illisible (python3 absent du nœud ?)"}

    cpu = rap.get("cpu") or {}
    bench = rap.get("bench") or {}
    modele = (cpu.get("model_name") or "").strip()
    if not modele:
        return {"ok": False, "error": "modèle de CPU non identifiable sur ce nœud"}

    # Identité du nœud : c'est elle qui fait le lien avec la bibliothèque.
    try:
        db_update_node(node["id"], cpu_model=modele)
    except Exception as e:
        log.warning("qualify_node_cpu(%s): écriture cpu_model : %s", node.get("id"), e)

    # ★ Ni `quota_mbs`, ni `measured=1`. On enregistre ce qu'on a VRAIMENT constaté : l'identité et
    # le proxy. Un `quota_mbs` déjà présent (saisi à la main, ou ancré par une campagne) survit —
    # `db_upsert_cpu_profile` ignore les champs None.
    _horo = rap.get("horodatage") or ""
    note = ("identité + micro-banc relevés le %s — memcpy %.2f Go/s, parsing %.3f Mpps ; "
            "estimation INDICATIVE %s Mb/s (proxy NON ancré : aucune campagne de charge réelle). "
            "Le quota reste celui de la cascade tant qu'il n'est pas mesuré."
            % (_horo,
               float(bench.get("memcpy_gbps") or 0), float(bench.get("pkt_parse_mpps") or 0),
               (rap.get("quota_mbs_estime") or {}).get("valeur")))
    try:
        db_upsert_cpu_profile(
            modele,
            cores=cpu.get("coeurs_physiques"), threads=cpu.get("threads_logiques"),
            base_mhz=cpu.get("freq_base_mhz"),
            memcpy_gbps=bench.get("memcpy_gbps"), pkt_mpps=bench.get("pkt_parse_mpps"),
            notes=note)
    except Exception as e:
        return {"ok": False, "error": "écriture du profil CPU : %s" % e}

    prof = db_get_cpu_profile(modele) or {}
    _av = list(rap.get("avertissements") or [])
    if _coeur is None:
        _av.insert(0, "banc NON épinglé hors du moteur 2110 (aucun cœur libre identifié) — la mesure "
                      "peut refléter la contention des lcores en busy-poll plutôt que le processeur")
    return {"ok": True, "cpu_model": modele, "profil": prof, "coeur_banc": _coeur,
            "estimation": rap.get("quota_mbs_estime"), "avertissements": _av}
