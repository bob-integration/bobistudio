#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""L'alarme de QUALITÉ du servo PTP. On vérifie surtout les cas où elle doit se TAIRE :
une alarme qui sonne au démarrage ou en doublon est pire que pas d'alarme."""
import sys, time; sys.path.insert(0,'/opt/bobistudio')
from app import ptp
from app import database as db

events, alertes = [], []
db.db_add_ptp_event = lambda *a, **k: events.append(a)
db.db_add_alert     = lambda cle, niv="info", **k: alertes.append((cle, niv))
db.db_get_setting   = lambda k, d=None: 2.0 if k == "ptp_servo_warn_s" else d
ptp._ptp_alertes_actives = lambda: True

NODE = {"id": 34, "name": "dl360-1"}
def dom(locked, synced=True, engine=True):
    return {"network_id": 1, "name": "STC Bondy", "locked": locked, "synced": synced,
            "engine_ptp": engine, "ifaces_state": {}, "grandmaster_id": "GM1",
            "ptp4l_running": False}
def passe(d):
    n0 = len(alertes)
    ptp._detect_ptp_events(NODE, {"domains": [d]})
    return [a for a in alertes[n0:]]

def raz():
    ptp._ptp_event_state.clear(); ptp._servo_loose_since.clear(); ptp._servo_alerted.clear()
    ptp._unlocked_since.clear(); ptp._unlock_alerted.clear(); ptp._alerte_diffusee.clear()
    alertes.clear()

def cas(nom, attendu, fait):
    ok = (attendu == fait)
    print('%s %-44s attendu=%-8s obtenu=%s' % ('OK  ' if ok else '✕ KO', nom, attendu, fait))
    return ok

tout = True
# 1) Démarrage : servo non convergé mais SOUS le seuil → silence
raz(); passe(dom(False))                       # seed muet
a = passe(dom(False))
tout &= cas("servo non convergé, sous le seuil", [], [x[0] for x in a])

# 2) Au-delà du seuil → une alarme, UNE seule
time.sleep(2.2)
a = passe(dom(False))
tout &= cas("au-delà du seuil", ['alert.ptp.servo_non_converge'], [x[0] for x in a])
a = passe(dom(False))
tout &= cas("passe suivante : pas de répétition", [], [x[0] for x in a])

# 3) Reconvergence → retour à la normale
a = passe(dom(True))
tout &= cas("servo reconvergé", ['alert.ptp.servo_reconverge'], [x[0] for x in a])

# 4) Nœud ptp4l (pas de PTP moteur) : jamais cette alarme
raz(); passe(dom(False, engine=False)); time.sleep(2.2)
a = passe(dom(False, engine=False))
tout &= cas("nœud ptp4l : hors sujet", [], [x[0] for x in a if 'servo' in x[0]])

# 5) Horloge ABSENTE : la disponibilité prime, pas d'alarme de qualité
raz(); passe(dom(False, synced=False)); time.sleep(2.2)
a = passe(dom(False, synced=False))
tout &= cas("horloge absente : la qualité se tait", [], [x[0] for x in a if 'servo' in x[0]])

# 6) Servo convergé en permanence : silence total
raz(); passe(dom(True)); time.sleep(2.2)
a = passe(dom(True))
tout &= cas("servo convergé : silence", [], [x[0] for x in a])

print('\n' + ('✓ les six cas se comportent comme voulu' if tout else '✕ RÉGRESSION'))
sys.exit(0 if tout else 1)
