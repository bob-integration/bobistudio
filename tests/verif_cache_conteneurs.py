#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde : le cache de `db_get_containers` ne sert JAMAIS une table périmée (24/10).

Le cache est invalidé par `containers_gen.n` (compteur tenu par déclencheurs), lu par une connexion
sonde dédiée. Vérifie, sur une
base jetable :
  1. un second appel sans écriture ne relit pas la table (le cache sert vraiment), ni après une
     écriture sur une autre table ;
  2. une écriture validée du MÊME fil est vue au prochain appel ;
  3. une écriture d'un AUTRE fil est vue ;
  4. une écriture d'une connexion extérieure (autre processus, secours HA) est vue ;
  5. un fil en transaction ouverte voit sa propre écriture non validée ;
  6. annoter un dict rendu ne pollue pas l'appel suivant ;
  7. changer de base (`DB_PATH`) ne sert pas l'ancienne ;
  8. un fil qui lit pendant la mise à jour du cache n'obtient pas de lignes périmées ;
  9. réécrire une valeur identique (statut, fps, usage, IP) ne fait pas avancer le compteur.
"""
import os
import sqlite3
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import database  # noqa: E402

TMP = tempfile.mkdtemp()
database.DB_PATH = os.path.join(TMP, "a.db")
database.init_db()

fails = []


def check(cond, what, extra=None):
    print(("OK   " if cond else "FAIL ") + what + ("" if cond or extra is None else "  → %.300r" % (extra,)))
    if not cond:
        fails.append(what)


def hosts():
    return {c["vmid"]: c["hostname"] for c in database.db_get_containers()}


def write(sql, args=()):
    db = database.get_db()
    db.execute(sql, args)
    db.commit()


write("INSERT INTO containers (vmid, hostname) VALUES (1, 'a')")
selects = []
database.get_db().set_trace_callback(lambda q: selects.append(q) if "FROM containers ORDER" in q else None)

# 1.
check(hosts() == {1: "a"}, "1a. première lecture")
n = len(selects)
check(hosts() == {1: "a"} and len(selects) == n, "1. second appel servi par le cache", len(selects) - n)

# 1b.
write("INSERT INTO alerts (message, niveau) VALUES ('x', 'info')")
n = len(selects)
check(hosts() == {1: "a"} and len(selects) == n, "1b. une écriture sur une AUTRE table ne vide pas le cache",
      len(selects) - n)

# 2.
write("UPDATE containers SET hostname='b' WHERE vmid=1")
check(hosts() == {1: "b"}, "2. écriture du même fil vue")

# 3.
t = threading.Thread(target=lambda: write("INSERT INTO containers (vmid, hostname) VALUES (2, 'c')"))
t.start()
t.join()
check(hosts() == {1: "b", 2: "c"}, "3. écriture d'un autre fil vue")

# 4.
ext = sqlite3.connect(database.DB_PATH)
ext.execute("UPDATE containers SET hostname='d' WHERE vmid=2")
ext.commit()
ext.close()
check(hosts() == {1: "b", 2: "d"}, "4. écriture d'une connexion extérieure vue")

# 5.
db = database.get_db()
db.execute("UPDATE containers SET hostname='e' WHERE vmid=1")
check(db.in_transaction and hosts().get(1) == "e", "5. transaction ouverte : sa propre écriture vue")
db.commit()

# 6. (lecture préalable : l'annotation doit porter sur une réponse SERVIE PAR LE CACHE)
hosts()
database.db_get_containers()[0]["hostname"] = "pollué"
check(hosts().get(1) == "e", "6. un dict annoté ne pollue pas le cache")

# 8. Préemption juste après une affectation du cache : un autre fil qui lit à cet instant ne
# doit pas recevoir de lignes périmées (la clé posée avant les lignes le permettait).
write("UPDATE containers SET hostname='f' WHERE vmid=1")
seen = []


class Preempt(dict):
    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        t = threading.Thread(target=lambda: seen.append(hosts().get(1)))
        t.start()
        t.join()


real = database._ct_cache
database._ct_cache = Preempt(real)
hosts()
database._ct_cache = real
check(seen and all(x == "f" for x in seen), "8. lecture concurrente au milieu de la mise à jour du cache : fraîche", seen)

# 9. Les helpers rafraîchis à chaque tour n'écrivent que ce qui CHANGE : réécrire la même valeur
# ferait avancer `containers_gen` et viderait le cache pour rien (~8 fois/s mesuré le 24/10).
def gen():
    return database._containers_version()[1]


for nom, f in (("status", lambda: database.db_update_status(1, "running")),
               ("status+cause", lambda: database.db_update_status(1, "running", cause="")),
               ("fps", lambda: database.db_update_fps(1, 50.0)),
               ("usage", lambda: database.db_update_usage(1, 12.5, 1000)),
               ("usage+cpus", lambda: database.db_update_usage(1, 12.5, 1000, cpu_count=4)),
               ("ip", lambda: database.db_update_ip(1, "10.0.0.1")),
               ("docker_ip", lambda: database.db_update_docker_ip(1, "10.0.0.2"))):
    f()
    g0 = gen()
    f()
    check(gen() == g0, "9. %s : réécrire la même valeur n'avance pas le compteur" % nom, gen() - g0)
# fps et usage ne touchent plus du tout `containers` (table container_metrics, 24/10) : le cas d'une
# vraie modification se vérifie donc sur une colonne qui y reste, l'IP.
g0 = gen()
database.db_update_ip(1, "10.0.0.9")
check(gen() == g0 + 1 and database.db_get_container(1)["ip"] == "10.0.0.9", "9b. une vraie modification, elle, l'avance")
g0 = gen()
database.db_update_ip(1, None)
database.db_update_ip(1, None)
check(gen() == g0 + 1 and database.db_get_container(1)["ip"] is None, "9c. NULL compté une seule fois (IS NOT, pas !=)")

# 7.
database.DB_PATH = os.path.join(TMP, "b.db")
database._tls.conn = None
database.init_db()
check(hosts() == {}, "7. autre base : l'ancienne n'est pas servie", hosts())
# Les compteurs des deux sondes peuvent coïncider : c'est le chemin dans la clé qui les sépare.
check(database._containers_version()[0] == database.DB_PATH, "7b. la clé du cache porte le chemin de la base")

if fails:
    print("\nÉCHEC — %d vérification(s)" % len(fails))
    sys.exit(1)
print("\nOK — cache de db_get_containers exact")
