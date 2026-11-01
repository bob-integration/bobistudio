#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Garde du REAPER des moniteurs utilisateur (app/monitor.py), hors ligne.
#
# ★ CE QUE ÇA PROTÈGE : un encodeur de monitoring que personne ne regarde est coupé après
# IDLE_TIMEOUT. Mesuré le 2026-10-24, il ne l'était jamais, pour deux raisons cumulées :
#   - `_last_used` vit en mémoire : après un redémarrage de l'orchestrateur il est vide, et le
#     reaper ne parcourt que lui → les encodeurs déjà lancés n'étaient plus jamais candidats ;
#   - un /stop sans INTENTION (`containers.script_enabled`) est défait par la surveillance, qui
#     relance tout script arrêté : 1207 coupé à 11:09:35, « Script repris » à 11:09:48.
#   1207 tournait ainsi sans spectateur et entretenait seul un lien RDMA vers r620-2.
#
# ★ CE QUI EST VÉRIFIÉ :
#   1. au démarrage, l'horloge d'inactivité est armée pour chaque moniteur utilisateur existant
#      (`monitor_user_id`, ou hostname historique `monitor-u<uid>`), pas pour un monitor dédié ;
#   2. le reaper pose l'intention « arrêté » AVANT le /stop, et seulement sur un moniteur inactif ;
#   3. activate, set_source (redéploiement) et create_iter reposent l'intention « actif » AVANT
#      de démarrer — sinon un moniteur une fois coupé ne redémarrerait plus jamais.
#
#   $ ./venv/bin/python tests/verif_monitor_reaper.py
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database as _database                                  # noqa: E402
TMP = tempfile.mkdtemp(prefix="verif_monreaper_")
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()

from app import monitor as mon                                         # noqa: E402
from app.database import db_script_enabled, get_db                     # noqa: E402

ECHECS = []


def ok(cond, msg):
    print(("  ok  " if cond else "  ÉCHEC ") + msg)
    if not cond:
        ECHECS.append(msg)


# ── Parc factice : deux moniteurs utilisateur (moderne + historique) et un monitor dédié ──
with get_db() as db:
    for vmid, host, uid in [(901, "Monitoring-Alice", 7), (902, "monitor-u8", None),
                            (903, "mon_projet_player", None)]:
        db.execute("INSERT INTO containers (vmid, hostname, ip, monitor_user_id) VALUES (?,?,?,?)",
                   (vmid, host, "10.0.0.%d" % (vmid - 900), uid))
        db.execute("UPDATE containers SET script_enabled=1 WHERE vmid=?", (vmid,))

# ── Agent factice : journal ordonné des gestes (intention ET appels à l'agent) ──
JOURNAL = []
RUNNING = {901: True, 902: True, 903: True}
_vmid_of_ip = {"10.0.0.1": 901, "10.0.0.2": 902, "10.0.0.3": 903}


class _Session:
    def post(self, url, **kw):
        ip = url.split("//")[1].split(":")[0]
        v = _vmid_of_ip[ip]
        geste = url.rsplit("/", 1)[1]
        JOURNAL.append((geste, v, db_script_enabled(v)))     # intention LUE au moment de l'appel
        RUNNING[v] = (geste == "start")

        class R:
            status_code = 200
        return R()


mon.deploy.agent_session = lambda: _Session()
mon.deploy.agent_url = lambda ip, path, **kw: "https://%s:8081%s" % (ip, path)
mon.deploy.agent_headers = lambda vmid: {}
mon._script_running = lambda ip, vmid=None: RUNNING.get(vmid, False)
DEPLOYES = []
mon.deployer_script = lambda vmid, t, params: DEPLOYES.append((vmid, db_script_enabled(vmid))) or True
mon.gateway_ready = lambda: True

print("1. horloge d'inactivité armée au démarrage")
mon._last_used.clear()
T0 = 1_000_000.0
mon._seed_idle_clock(now=T0)
ok(mon._last_used.get(7) == T0, "moniteur moderne (monitor_user_id=7) armé")
ok(mon._last_used.get(8) == T0, "moniteur historique (monitor-u8) armé")
ok(len(mon._last_used) == 2, "le monitor dédié n'est pas armé (jamais reapé)")
mon._last_used[7] = T0 + 5
mon._seed_idle_clock(now=T0 + 100)
ok(mon._last_used[7] == T0 + 5, "une activité réelle n'est pas écrasée par un nouvel armement")

print("2. le reaper pose l'intention AVANT le /stop")
_now = mon.time.time
mon.time.time = lambda: T0 + mon.IDLE_TIMEOUT + 2       # 7 : T0+5 → encore actif ; 8 : inactif
JOURNAL.clear()
mon._reap_once()
mon.time.time = _now
ok(("stop", 902, False) in JOURNAL, "902 inactif : /stop envoyé avec l'intention déjà à « arrêté »")
ok(not any(v == 901 for _g, v, _i in JOURNAL), "901 encore actif : rien n'est touché")
ok(db_script_enabled(902) is False, "intention persistée : la surveillance ne le relancera pas")
ok(db_script_enabled(901) is True, "l'intention de 901 reste « actif »")
ok(8 not in mon._last_used, "902 sort de la liste du reaper une fois coupé")

print("3. réactivation : l'intention « actif » est reposée AVANT de démarrer")
JOURNAL.clear()
mon.activate(8)
ok(("start", 902, True) in JOURNAL, "activate : /start avec l'intention déjà à « actif »")

with get_db() as db:
    db.execute("UPDATE containers SET script_enabled=0 WHERE vmid=901")
mon._shm_fmt = lambda shm: None                 # format inconnu → chemin de redéploiement
mon._shm_fmt_node = lambda c, shm: None
DEPLOYES.clear()
mon.set_source(7, "src_video")
mon_threads = [t for t in __import__("threading").enumerate() if t.daemon and t.is_alive()]
for t in mon_threads:
    t.join(timeout=2)
ok((901, True) in DEPLOYES, "set_source : redéploiement avec l'intention déjà à « actif »")

with get_db() as db:
    db.execute("UPDATE containers SET script_enabled=0 WHERE vmid=902")
DEPLOYES.clear()
list(mon.create_iter(8))
ok((902, True) in DEPLOYES, "create_iter (conteneur réutilisé) : intention « actif » avant le déploiement")

print()
if ECHECS:
    print("ÉCHEC : %d assertion(s)" % len(ECHECS))
    sys.exit(1)
print("TOUT VERT")
