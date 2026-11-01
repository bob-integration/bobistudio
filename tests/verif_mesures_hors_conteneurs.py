#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde : les mesures vivantes (fps, CPU, mémoire) vivent hors de `containers` (24/10).

Écrites dans `containers`, elles faisaient avancer `containers_gen` à chaque relevé et vidaient le
cache de `db_get_containers` ~4,5 fois/s. Vérifie, sur une base jetable :
  1. un relevé de fps ou d'usage n'avance PAS le compteur du cache ;
  2. une lecture servie par le cache porte quand même la mesure la plus récente ;
  3. db_get_container et db_container_by_instance la portent aussi ;
  4. réécrire la même mesure n'écrit rien ;
  5. au démarrage, les dernières valeurs de `containers` sont reprises une fois, sans jamais
     écraser une mesure plus récente ;
  6. supprimer le conteneur supprime ses mesures, et un relevé tardif ne la recrée pas ;
  7. au démarrage, les mesures sans conteneur sont effacées.
"""
import os
import sqlite3
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import database  # noqa: E402

TMP = tempfile.mkdtemp()
database.DB_PATH = os.path.join(TMP, "m.db")

# Base d'AVANT : des mesures écrites dans `containers` par l'ancien code.
database.init_db()
old = sqlite3.connect(database.DB_PATH)
old.execute("DROP TABLE container_metrics")
old.execute("INSERT INTO containers (vmid, hostname, instance_uuid, fps, cpu_percent, mem_used, cpu_count) "
            "VALUES (1, 'a', 'uuid-a', 25.0, 12.5, 1000, 4)")
old.commit()
old.close()
database.init_db()

fails = []


def check(cond, what, extra=None):
    print(("OK   " if cond else "FAIL ") + what + ("" if cond or extra is None else "  → %.300r" % (extra,)))
    if not cond:
        fails.append(what)


def gen():
    return database._containers_version()[1]


def one():
    return [c for c in database.db_get_containers() if c["vmid"] == 1][0]


c = one()
check((c["fps"], c["cpu_percent"], c["mem_used"], c["cpu_count"]) == (25.0, 12.5, 1000, 4),
      "5. reprise des mesures écrites dans containers", c)

# 1. + 2.
g0 = gen()
selects = []
database.get_db().set_trace_callback(lambda q: selects.append(q) if "FROM containers ORDER" in q else None)
one()
n = len(selects)
database.db_update_fps(1, 50.0)
database.db_update_usage(1, 80.0, 2000, cpu_count=8)
check(gen() == g0, "1. un relevé de fps / d'usage n'avance pas containers_gen", gen() - g0)
c = one()
check(len(selects) == n, "2. la lecture suivante est servie par le cache", len(selects) - n)
check((c["fps"], c["cpu_percent"], c["mem_used"], c["cpu_count"]) == (50.0, 80.0, 2000, 8),
      "2b. … et porte pourtant la mesure la plus récente", c)
database.get_db().set_trace_callback(None)

# 3.
c1 = database.db_get_container(1)
c2 = database.db_container_by_instance("uuid-a")
check(c1["fps"] == 50.0 and c1["mem_used"] == 2000, "3. db_get_container porte les mesures", c1)
check(c2["fps"] == 50.0 and c2["cpu_percent"] == 80.0, "3b. db_container_by_instance aussi", c2)

# 4.
db = database.get_db()
t0 = db.total_changes
database.db_update_fps(1, 50.0)
database.db_update_usage(1, 80.0, 2000, cpu_count=8)
database.db_update_usage(1, 80.0, 2000)
check(db.total_changes == t0, "4. réécrire la même mesure n'écrit rien", db.total_changes - t0)
database.db_update_usage(1, 81.0, 2000)
check(db.total_changes == t0 + 1, "4b. une vraie variation, elle, s'écrit", db.total_changes - t0)

# 5b. Un redémarrage ne remet pas les vieilles valeurs de `containers` par-dessus.
database.init_db()
c = one()
check(c["fps"] == 50.0 and c["cpu_percent"] == 81.0, "5b. redémarrage : la mesure récente reste", c)

# 6.
database.db_delete_container(1)
left = database.get_db().execute("SELECT COUNT(*) FROM container_metrics WHERE vmid=1").fetchone()[0]
check(left == 0, "6. supprimer le conteneur supprime ses mesures", left)

# 6b. Un relevé arrivé après la destruction ne recrée pas la ligne.
database.db_update_fps(1, 25.0)
database.db_update_usage(1, 10.0, 500)
database.db_update_usage(1, 10.0, 500, cpu_count=2)
left = database.get_db().execute("SELECT COUNT(*) FROM container_metrics WHERE vmid=1").fetchone()[0]
check(left == 0, "6b. un relevé d'un conteneur détruit n'écrit rien", left)

# 7. Au démarrage, les mesures sans conteneur sont effacées (25/10 : 18 lignes de Régie Légère).
database.get_db().execute("INSERT INTO container_metrics (vmid, fps) VALUES (999, 50.0)")
database.get_db().commit()
database.init_db()
left = database.get_db().execute("SELECT COUNT(*) FROM container_metrics WHERE vmid=999").fetchone()[0]
check(left == 0, "7. démarrage : mesures orphelines effacées", left)

if fails:
    print("\nÉCHEC — %d vérification(s)" % len(fails))
    sys.exit(1)
print("\nOK — mesures vivantes hors de containers")
