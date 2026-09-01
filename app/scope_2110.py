# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Relais du relevé 2110 du moteur vers les scopes qui mesurent une source 2110.

## Pourquoi un relais, et pas une lecture directe

⚠ **LE SCOPE NE PEUT PAS INTERROGER LE MOTEUR.** Il est en macvlan (une IP du subnet du cluster),
le moteur `2110_io` est sur le réseau HÔTE du nœud, et un conteneur macvlan ne joint pas son
propre hôte — c'est une propriété du pilote, pas un réglage. Vérifié au banc le 2026-08-26 :
`urlopen("http://x.x.x.x:8080/")` depuis `bobi-cmp-1028` part en timeout, alors que
l'orchestrateur lit la même URL en 20 ms. Seul l'orchestrateur voit les deux côtés. Ce n'est
donc pas un détour : c'est la seule route.

## Ce que le scope mesure sans ce relais, et ce qu'il lui manque

L'instrument de phase lit deux choses sur le bus MXL : l'instant NOMINAL du dernier grain
(`index_time_ns`, la grille TAI) et l'instant d'ÉCRITURE (`last_write_time`). Les deux sont
mesurés APRÈS le moteur : tout ce qui se passe sur le fil — l'instant du premier paquet dans la
trame, la conformité 2110-21, la latence RX, l'état du PTP — lui est invisible.

Or le moteur mesure déjà tout cela, par récepteur, et le publie sur son `:8080` :
`fpt` (first packet time, ns depuis l'époque de trame — la PHASE 2110 de la source),
`vrx_*` et `cinst_*` (modèle de tampon ST 2110-21), `rx_latency_ms`, `late`, `compliant` /
`failed_cause`, `signal` (black/frozen/gamut), et à la racine le bloc `ptp` (verrou, domaine,
écart au GM, identité du GM).

Le travail ici n'est donc PAS de mesurer le 2110 — il l'est déjà — mais de l'acheminer.

## La cadence, et pourquoi elle est lente

Une fois par seconde, et UNE SEULE lecture par moteur quel que soit le nombre de scopes qui en
dépendent. Ce sont des grandeurs de SOURCE (phase sur le fil, conformité, verrou PTP) : elles ne
bougent pas à la trame, et le moteur lui-même ne rafraîchit ses compteurs qu'à la seconde.
Interroger plus vite ne donnerait rien de plus et ajouterait de la charge à un moteur qui tourne
sur des cœurs isolés.
"""
import json
import logging
import threading
import time

log = logging.getLogger(__name__)

_started = False
# Dernier relevé par vmid de moteur, pour ne pas relire le même moteur une fois par scope.
_cache = {}
INTERVALLE = 1.0
PERIME_S = 10.0


def _containers_par_type():
    """(scopes, moteurs) — deux listes de (container, params). Un seul balayage de la flotte."""
    from .database import db_get_containers
    scopes, moteurs = [], []
    for c in db_get_containers():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:                                          # noqa: BLE001
            continue
        t, p = dc.get("type"), (dc.get("params") or {})
        if t == "scope":
            scopes.append((c, p))
        elif t == "2110_io":
            moteurs.append((c, p))
    return scopes, moteurs


def _origine_2110(shm, moteurs):
    """(container du moteur, idx 0-based du récepteur) qui produit `shm`, ou (None, None).

    ⚠ LE NOM DU FLUX EST 1-BASED, L'INDICE DE TABLEAU EST 0-BASED (cf. `app/numerotation.py`).
    On ne dérive donc pas l'un de l'autre à la main : on demande son nom à `flux_video` pour
    chaque flux actif et on compare. Un `int(shm.rsplit('_')[-1]) - 1` aurait marché aujourd'hui
    et se serait décalé au prochain changement de convention — c'est exactement la faute que ce
    module de numérotation existe pour empêcher."""
    if not shm:
        return None, None
    from . import io2110_flows as _iof
    from .numerotation import flux_video as _fv
    for c, p in moteurs:
        hn = p.get("hostname") or c.get("hostname") or ""
        for f in _iof.active_flows(p, "rx"):
            if f.get("essence") == "video" and _fv(hn, f["idx"]) == shm:
                return c, int(f["idx"])
    return None, None


def _nic_utile(nic):
    """Le strict nécessaire du bloc NIC, ports compris. Les compteurs `mtl_stats` sont CUMULÉS
    depuis le démarrage du moteur : c'est le consommateur qui en fait des deltas, pas nous —
    faire la différence ici obligerait le relais à garder un état par moteur pour rien."""
    out = {k: nic.get(k) for k in ("rx_gbps", "tx_gbps", "model", "port_capacity_gbps")}
    out["ports"] = [{
        "iface": p.get("iface"), "pmd": p.get("pmd"), "primary": p.get("primary"),
        "rx_gbps": p.get("rx_gbps"), "link_up": p.get("link_up"),
        "stats": {k: (p.get("mtl_stats") or {}).get(k)
                  for k in ("rx_packets", "rx_err", "rx_hw_dropped", "rx_nombuf")},
    } for p in (nic.get("ports") or [])]
    return out


def _declare(moteur_vmid, idx):
    """Ce que la source DÉCLARE pour ce récepteur, décomposé, ou None.

    Le SDP vient de l'état IS-05 vivant (`active.transport_file.data`) et non d'un fichier lu
    dans le conteneur : c'est l'orchestrateur qui l'y a poussé, il en est la source. Passer par
    le disque du moteur ajouterait un aller-retour par flux et par seconde pour retrouver ce
    qu'on a déjà en mémoire."""
    try:
        from services import nmos as _nmos
        from . import sdp2110
        return sdp2110.lire(_nmos.active_sdp_for(moteur_vmid, idx, "video"))
    except Exception as e:                                         # noqa: BLE001
        log.debug("scope_2110 : SDP du récepteur %s : %s", idx, e)
        return None


def _lire_moteur(c):
    """Relevé complet d'un moteur, mémoïsé une seconde. None si injoignable."""
    import requests
    vmid = c["vmid"]
    ent = _cache.get(vmid)
    now = time.monotonic()
    if ent and (now - ent[0]) < INTERVALLE:
        return ent[1]
    ip = c.get("ip")
    d = None
    if ip:
        try:
            r = requests.get("http://%s:8080/" % ip, timeout=2)
            if r.status_code == 200:
                d = r.json()
        except Exception as e:                                     # noqa: BLE001
            log.debug("scope_2110 : moteur #%s injoignable : %s", vmid, e)
    _cache[vmid] = (now, d)
    return d


def _pousser(scope_c, corps):
    from . import deploy
    ip = scope_c.get("ip")
    if not ip:
        return
    import requests
    try:
        requests.post("http://%s:8082/source2110" % ip, json=corps, timeout=2,
                      headers=deploy.agent_headers(scope_c["vmid"]))
    except Exception as e:                                         # noqa: BLE001
        log.debug("scope_2110 : scope #%s injoignable : %s", scope_c["vmid"], e)


def pousser_une_fois():
    """Un tour. Retourne le nombre de scopes servis (0 = rien à faire, pas une erreur)."""
    scopes, moteurs = _containers_par_type()
    if not scopes or not moteurs:
        return 0
    n = 0
    for sc, sp in scopes:
        mot, idx = _origine_2110((sp.get("input") or "").strip(), moteurs)
        if mot is None:
            # La source n'est PAS un flux 2110 (un mur, un lecteur, un mélangeur…). On le DIT au
            # scope au lieu de nous taire : sans ce message, l'instrument ne peut pas distinguer
            # « pas de 2110 en amont » de « relais en panne », et il afficherait un cadran vide
            # dans les deux cas — deux situations qui n'appellent pas la même réaction.
            _pousser(sc, {"origine": None, "shm": (sp.get("input") or "").strip()})
            continue
        d = _lire_moteur(mot)
        if not d:
            continue
        rec = None
        for r in (d.get("receivers") or []):
            if int(r.get("idx", -1)) == idx and r.get("essence") == "video":
                rec = r
                break
        if rec is None:
            _pousser(sc, {"origine": None, "shm": (sp.get("input") or "").strip()})
            continue
        _pousser(sc, {"origine": {"moteur": mot.get("hostname") or mot["vmid"],
                                  "numero": rec.get("numero"), "idx": idx,
                                  # ⚠ LE FLUX SUR LEQUEL CE RELEVÉ A ÉTÉ FAIT, pour que le
                                  # conteneur puisse le REFUSER s'il n'écoute plus celui-là.
                                  # Le relais lit l'entrée dans la BASE ; un recâblage à chaud
                                  # par `/input` ne la met pas à jour, et le scope affichait
                                  # alors les mesures 2110 de son ANCIENNE source — « conforme
                                  # narrow » sur un lecteur qui n'a jamais vu de 2110. C'est
                                  # exactement la famille de fautes qu'on traque, et celle-ci
                                  # était la mienne.
                                  "shm": (sp.get("input") or "").strip()},
                      "recepteur": rec, "ptp": d.get("ptp") or {},
                      # ⚠ CE QUE LA SOURCE DÉCLARE, pour être CONFRONTÉ — jamais pour être
                      # affiché tel quel. Un SDP est une affirmation de l'émetteur sur
                      # lui-même ; le relayer sans la confrontation serait une régression de
                      # doctrine, pas un progrès. `app/sdp2110.py` attache à chaque champ la
                      # façon dont il peut être vérifié, et le conteneur s'en sert.
                      "declare": _declare(mot["vmid"], idx),
                      # ⚠ LES COMPTEURS DE PAQUETS SONT PAR PORT, PAS PAR FLUX — et le relais
                      # les jetait. On les transmet maintenant, MAIS avec le nom du port, parce
                      # que c'est la seule façon d'empêcher qu'un « 9 478 paquets jetés »
                      # agrégé sur quatorze sessions se lise comme le compteur d'un seul flux.
                      # Les compteurs PAR SESSION n'existent pas encore côté moteur : c'est un
                      # ajout à `mtl_rx.c`, pas à la tuile, et la tuile doit le DIRE.
                      "nic": _nic_utile(d.get("nic") or {})})
        n += 1
    return n


def start(interval=INTERVALLE):
    """Thread daemon (idempotent). Lancé depuis main.py."""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        while True:
            try:
                from . import ha as _ha
                if _ha.is_active():
                    pousser_une_fois()
            except Exception as e:                                 # noqa: BLE001
                log.warning("scope_2110 : %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="scope-2110").start()
