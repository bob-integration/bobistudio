# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Collecte des ÉVÉNEMENTS INTERNES des plugins vers le journal d'exploitation.

Ce que l'orchestrateur ne voit pas passer (pour la console : saturations, flux d'entrée perdus ou
retrouvés, blocs en retard, refus du temps réel, démarrages) est publié PAR LE PLUGIN sur
`GET /events?since=<ms>` et rangé dans la MÊME chronologie que les gestes (auteur « plugin »).
Format : docs/reference/PLUGIN_JOURNAL.md § Événements.

★ SEULEMENT CE QUI EST DÉCLARÉ. Le collecteur ne parle qu'aux instances en marche dont le plugin
déclare `control.journal.events` (la console depuis 0.4.79) ; sans déclaration, aucune requête.

★ `boot` NOMME LA SÉQUENCE PERSISTÉE, PAS LE PROCESSUS (PLUGIN_JOURNAL.md § 6) : il est recopié tel
que la réponse le donne sur chaque événement ; un redémarrage du plugin garde le même `boot` et fait
sauter `seq` — la clé (boot, seq) d'un événement rechargé reste celle de sa première publication.

★ L'ACCÈS AU MONDE EST UNE INTERFACE (`Source`) : le banc lui substitue un faux, le collecteur n'a
jamais besoin d'un conteneur pour être vérifié. Idempotence portée par la base (clé unique
`boot:seq` par instance) : relire une fenêtre qui chevauche la précédente ne double rien.
"""
import json
import logging
import threading

log = logging.getLogger(__name__)

POLL_S = 5.0
OVERLAP_MS = 2000          # on relit un peu en arrière : une horloge qui recule d'un pas ne perd rien
HTTP_TIMEOUT_S = 2.0
MAX_EVENTS_PER_POLL = 500


class Source:
    """Accès au monde. `instances()` → [(vmid, manifeste, spec events)] ; `fetch(vmid, spec, since)`
    → la réponse JSON de `GET <endpoint>?since=<since>` (ou lève)."""

    def instances(self):
        from . import plugins
        from . import ops_journal_rules as rules
        from .database import db_get_containers
        out = []
        for c in db_get_containers() or []:
            if c.get("status") not in (None, "running"):
                continue
            try:
                dc = json.loads(c.get("deploy_config") or "{}") if isinstance(c.get("deploy_config"), str) \
                    else (c.get("deploy_config") or {})
            except ValueError:
                continue
            m = plugins.get(dc.get("type"))
            sp = rules.spec(m) if m else None
            if sp and sp.get("events"):
                out.append((c["vmid"], m, sp["events"]))
        return out

    def fetch(self, vmid, spec, since_ms):
        import requests
        from .addressing import get_container_ip
        ip = get_container_ip(vmid)
        if not ip:
            raise ConnectionError("no IP")
        port = int(spec.get("port") or 8082)
        r = requests.get("http://%s:%d%s" % (ip, port, spec["endpoint"]), params={"since": int(since_ms)},
                         timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError("%s → HTTP %s" % (spec["endpoint"], r.status_code))
        return r.json()


class Collector:
    def __init__(self, source=None, record=None):
        self.source = source or Source()
        if record is None:
            from . import ops_journal
            record = ops_journal.record_event
        self.record = record
        self.cursor = {}         # vmid -> {"rev", "since", "boot"}
        self.errors = 0
        self.last_error = None

    def tick(self):
        """Une passe. Rend le nombre d'événements transmis au journal."""
        n = 0
        live = set()
        for vmid, m, spec in self.source.instances():
            live.add(vmid)
            cur = self.cursor.setdefault(vmid, {"rev": None, "since": 0, "boot": None})
            try:
                data = self.source.fetch(vmid, spec, max(0, cur["since"] - OVERLAP_MS))
            except Exception as e:
                self.errors += 1
                self.last_error = "#%s: %s" % (vmid, e)
                continue
            if not isinstance(data, dict):
                continue
            boot = data.get("boot")
            if boot != cur["boot"]:
                cur.update(boot=boot, rev=None)            # plugin redémarré : nouvelle séquence
            if data.get("rev") is not None and data.get("rev") == cur["rev"]:
                continue
            for ev in (data.get("events") or [])[:MAX_EVENTS_PER_POLL]:
                if not isinstance(ev, dict) or ev.get("seq") is None:
                    continue
                ev = dict(ev, boot=boot)
                self.record(vmid, ev, m.get("type"))
                n += 1
                try:
                    cur["since"] = max(cur["since"], int(ev.get("ts") or 0))
                except (TypeError, ValueError):
                    pass
            cur["rev"] = data.get("rev")
        for v in list(self.cursor):
            if v not in live:
                del self.cursor[v]
        return n


COLLECTOR = None
_thread = None
_stop = threading.Event()


def start():
    """Boucle de collecte (contrôleur ACTIF seul, cf. main.py). Sans plugin qui publie : à vide."""
    global COLLECTOR, _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    COLLECTOR = Collector()

    def _loop():
        while not _stop.wait(POLL_S):
            try:
                COLLECTOR.tick()
            except Exception:
                log.exception("ops_journal_events: collection pass failed")

    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="ops-journal-events")
    _thread.start()
    return _thread


def stop():
    _stop.set()
