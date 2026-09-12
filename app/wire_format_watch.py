# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Vérification différée du format des câbles POSÉS EN PRÉ-CÂBLAGE.

## Le problème

`cabling._format_gate` refuse un câble dont la source n'a pas le format attendu par le
consommateur. Mais il compare ce qu'il TROUVE : le flow_def MXL réel quand le flux existe, et à
défaut le format DÉCLARÉ du producteur. Or déclarer n'est pas mesurer. Tant qu'une source n'est pas
en service (moteur pas démarré, RX sans signal), son format déclaré est une INTENTION — souvent
juste, parfois périmée, jamais un constat. Refuser sur cette base interdisait de préparer un patch
avant la mise en service, ce qui est un geste d'exploitation parfaitement normal.

## Ce qu'on fait

Écart sur un format seulement DÉCLARÉ ⇒ le câble est **posé**, avec une alerte qui le dit, et la
paire (source, consommateur) est **inscrite ici**. Ce module attend que le flux MXL apparaisse, puis
tranche sur le format RÉEL :

  · conforme      → alerte `info` (le pré-câblage était bon) et on oublie la paire ;
  · écart confirmé→ alerte `error` NOMMANT les deux formats — le câble reste en place, à l'opérateur
                    d'insérer un UDC ou d'aligner la source. On n'ôte pas un câble sous les pieds
                    d'une régie sans qu'elle l'ait demandé.

Sans cette repasse, tolérer le pré-câblage reviendrait à troquer un refus explicite contre un échec
silencieux : une image fausse qui ressemble à une image. C'est précisément ce que le gate existe
pour empêcher.

## Ce qu'on NE fait PAS

Pas d'insertion automatique d'UDC ici, contrairement à `tx_format_watch` : sur une sortie TX 2110,
l'invariant « format émis = format annoncé » est une exigence de CONFORMITÉ que le système doit
rétablir seul. Vers un consommateur logiciel, l'écart n'engage que l'image de ce consommateur, et
le choix (convertir, ou corriger la source) appartient à l'exploitant.

Les slots TX du moteur 2110 ne passent jamais par ici : leur gate (`cabling._tx_slot_mismatch`)
refuse SEC, déclaré ou mesuré — un écart y recrée la session et fige toutes les sorties de la carte.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

_started = False

from .episodes import EtatEpisodes as _Episodes
# (to_vmid, shm) → {"from_vmid", "type", "why"}. PERSISTÉ : un pré-câblage posé le vendredi doit
# être vérifié le lundi, redémarrages de l'orchestrateur compris.
_attente = _Episodes("wire_format_pending")


def inscrire(from_vmid, to_vmid, shm, to_type, why):
    """Note un câble posé malgré un écart de format PRÉDIT (source pas encore en service)."""
    try:
        _attente.poser((int(to_vmid), shm),
                       {"from_vmid": int(from_vmid or 0), "type": to_type or "", "why": why or ""})
    except Exception as e:
        log.warning("pré-câblage %s → #%s non inscrit (%s) — l'écart ne sera PAS re-vérifié.",
                    shm, to_vmid, e)


def purger_episodes():
    """Retire les paires dont le consommateur a disparu (appelée par la boucle de ce module). Sans
    ça le fichier d'état grossit indéfiniment, et un vmid RECYCLÉ hériterait d'une attente qui n'est
    pas la sienne — donc d'une alerte « écart confirmé » sur un câble qu'il n'a jamais eu."""
    try:
        from .database import db_get_containers
        vivants = {str(c.get("vmid")) for c in (db_get_containers() or [])}
    except Exception:
        return
    _attente.purger(lambda cle: cle.split("\x1f")[0] in vivants)


def _fmt(f):
    return "%sx%s%s%s" % (f.get("width"), f.get("height"), f.get("scan") or "p", f.get("fps"))


def scan_once():
    """Un tour. Retourne la liste des écarts CONFIRMÉS (dicts) sur ce tour."""
    from .database import db_add_alert, db_get_container
    from .routes.cabling import (_flow_def_format, _format_gate, _load_dc,
                                 _collect_current_edges)
    confirmes = []
    cles = _attente.cles()
    if not cles:
        return confirmes
    # Un câble défait n'a plus rien à vérifier : on lâche la paire au lieu d'alerter sur un
    # câblage qui n'existe plus.
    try:
        vivants = {(int(e.get("to_vmid")), e.get("shm")) for e in (_collect_current_edges() or [])}
    except Exception as e:
        log.warning("wire_format_watch : câbles courants illisibles (%s) — tour sauté.", e)
        return confirmes
    for cle in cles:
        try:
            _tv, _shm = cle.split("\x1f", 1)
            to_vmid = int(_tv)
        except ValueError:
            _attente.retirer(cle)
            continue
        ent = _attente.get(cle) or {}
        if (to_vmid, _shm) not in vivants:
            _attente.retirer(cle)
            continue
        from_vmid = ent.get("from_vmid") or 0
        if not _flow_def_format(from_vmid, _shm):
            continue                            # flux toujours pas créé : on attend, sans bruit
        c = db_get_container(to_vmid)
        dc = _load_dc(c) if c else None
        if not dc or not dc.get("type"):
            _attente.retirer(cle)
            continue
        # Le format du producteur est maintenant MESURÉ → `_format_gate` rend "refuse" en cas
        # d'écart réel (et "ok" si le pré-câblage était bon).
        verdict, why = _format_gate(from_vmid, _shm, dc["type"], dc.get("params") or {})
        hn = (c.get("hostname") or "#%s" % to_vmid)
        if verdict == "refuse":
            confirmes.append({"to_vmid": to_vmid, "shm": _shm, "from_vmid": from_vmid, "why": why})
            db_add_alert("alert.net.precablage_ecart_confirme", "error", vmid=to_vmid, kind="deploy",
                        params={"shm": _shm, "hn": hn, "why": why})
        else:
            db_add_alert("alert.net.precablage_conforme", "info", vmid=to_vmid, kind="deploy",
                        params={"shm": _shm, "hn": hn})
        _attente.retirer(cle)                   # tranché : la paire sort de l'attente
    _attente.flush(force=True)
    return confirmes


def start(interval=60):
    """Thread daemon de vérification (idempotent). Lancé depuis main.py."""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        while True:
            try:
                purger_episodes()
                scan_once()
            except Exception as e:
                log.warning("wire_format_watch: %s", e)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="wire-format-watch").start()
