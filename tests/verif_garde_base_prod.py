#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Garde de la GARDE : un test n'écrit jamais dans la base de production (app/database.py,
# `connect_guarded`), hors ligne.
#
# ★ CE QUE ÇA PROTÈGE : le 2026-10-24, 16 fichiers de tests/ écrivaient dans la VRAIE base à
# chaque lancement (réglages IS-07, labels, NMOS, init_db…). Lancés en parallèle par deux
# sessions, l'un a « restauré » la valeur posée par l'autre et la surface IS-07 de production est
# restée ouverte. Depuis, un script lancé depuis tests/ n'obtient la production qu'en LECTURE
# SEULE : il peut la lire, il échoue sur sa première écriture.
#
# ★ CE QUI EST VÉRIFIÉ (avec une FAUSSE production dans un dossier jetable — la vraie n'est
#   jamais ouverte, et le test tourne là où elle n'existe pas, en CI) :
#   1. ce script est bien reconnu comme lancé depuis tests/ ;
#   2. la base de production s'ouvre en lecture : lire marche, écrire lève ;
#   3. une base jetable reste en lecture-écriture ;
#   4. la production est celle FIGÉE par la config (`PRODUCTION_DB_PATH`), pas la valeur courante
#      de DB_PATH qu'un test réassigne — sinon le chemin de test serait pris pour la production ;
#   5. le registre des médias (db_medias.db, voisin de la production) est gardé de la même façon ;
#   6. hors tests (service lancé par `python main.py`), aucune restriction ;
#   7. toute tentative d'écriture refusée est NOTÉE — c'est elle qui fera sortir en code 1 un test
#      dont le code avale l'exception (chemin d'alerting) ; celles de ce fichier sont voulues et
#      la liste est vidée avant la sortie ;
#   8. production ABSENTE (exécuteur de CI, machine neuve) : le test reçoit une base jetable
#      initialisée au lieu de « unable to open database file », et le fichier de production n'est
#      jamais créé.
#
#   $ ./venv/bin/python tests/verif_garde_base_prod.py
import os
import sqlite3
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_gardeprod_")
from app import config, database                                       # noqa: E402

FAUSSE_PROD = os.path.join(TMP, "prod", "db_bobistudio.db")
os.makedirs(os.path.dirname(FAUSSE_PROD))
for f in (FAUSSE_PROD, os.path.join(os.path.dirname(FAUSSE_PROD), "db_medias.db")):
    with sqlite3.connect(f) as c:
        c.execute("CREATE TABLE t (v TEXT)")
        c.execute("INSERT INTO t VALUES ('prod')")
config.PRODUCTION_DB_PATH = FAUSSE_PROD
JETABLE = os.path.join(TMP, "jetable.db")

ECHECS = []


def ok(cond, msg):
    print(("  ok  " if cond else "  ÉCHEC ") + msg)
    if not cond:
        ECHECS.append(msg)


def ecrit(conn):
    try:
        conn.execute("INSERT INTO t VALUES ('x')")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False


print("1. lancé depuis tests/")
ok(database.running_under_tests(), "running_under_tests() est vrai pour ce script")

print("2. production : lecture oui, écriture non")
c = database.connect_guarded(FAUSSE_PROD)
ok(c.execute("SELECT v FROM t").fetchone()[0] == "prod", "lecture de la production possible")
ok(not ecrit(c), "écriture dans la production refusée")
c.close()
with sqlite3.connect(FAUSSE_PROD) as c:
    ok(c.execute("SELECT count(*) FROM t").fetchone()[0] == 1, "la production n'a pas bougé")

print("3. base jetable : lecture-écriture")
c = database.connect_guarded(JETABLE)
c.execute("CREATE TABLE t (v TEXT)")
ok(ecrit(c), "écriture dans une base jetable possible")
c.close()
ok(os.path.exists(JETABLE), "la base jetable du test est bien CELLE qu'il a demandée (pas redirigée)")

print("4. la production est le chemin FIGÉ, pas DB_PATH courant")
config.DB_PATH = database.DB_PATH = JETABLE          # ce qu'un test fait pour s'isoler
database._tls.__dict__.pop("conn", None)
ok(ecrit(database.get_db()), "get_db sur la base de test réassignée : écriture possible")
database._tls.__dict__.pop("conn", None)
database.DB_PATH = FAUSSE_PROD                         # test qui a oublié de s'isoler
ok(not ecrit(database.get_db()), "get_db sur la production : écriture refusée")
database._tls.__dict__.pop("conn", None)

print("5. registre des médias gardé de la même façon")
from app import media_registry as mr                                   # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(os.path.dirname(FAUSSE_PROD), "db_medias.db")
mr._tls.__dict__.pop("conns", None)
try:
    mr.db()
    ok(False, "db_medias de production ouvert en écriture (son _init aurait dû échouer)")
except sqlite3.OperationalError as e:
    ok("readonly" in str(e), "db_medias de production : écriture refusée (%s)" % e)

print("6. hors tests : aucune restriction")
_argv, _main_file = sys.argv[:], getattr(sys.modules["__main__"], "__file__", None)
sys.argv = [os.path.join(RACINE, "main.py")]
sys.modules["__main__"].__file__ = os.path.join(RACINE, "main.py")
try:
    ok(not database.running_under_tests(), "python main.py n'est pas un test")
    c = database.connect_guarded(FAUSSE_PROD)
    ok(ecrit(c), "le service écrit normalement dans sa base")
    c.close()
finally:
    sys.argv = _argv
    sys.modules["__main__"].__file__ = _main_file

print("7. les tentatives d'écriture refusées sont notées (sortie en code 1 sinon)")
tables = {t for _a, t in database.production_write_attempts}
ok(len(database.production_write_attempts) >= 2, "tentatives notées : %d" % len(database.production_write_attempts))
ok("t" in tables, "la table visée est nommée")
database.production_write_attempts.clear()      # les écritures de CE banc sont voulues

print("8. production ABSENTE (CI, machine neuve) : base jetable, jamais d'erreur d'ouverture")
# Comme l'exécuteur de CI : le dossier existe (le dépôt), le FICHIER de base n'y est pas.
ABSENTE = os.path.join(TMP, "depot-sans-base", "db_bobistudio.db")
os.makedirs(os.path.dirname(ABSENTE))
config.PRODUCTION_DB_PATH = ABSENTE
try:
    c = database.connect_guarded(ABSENTE)
    c.execute("CREATE TABLE t (v TEXT)")
    ok(ecrit(c), "connect_guarded sur une production absente : base jetable inscriptible")
    c.close()
except sqlite3.Error as e:
    ok(False, "connect_guarded sur une production absente : %s" % e)
database.DB_PATH = ABSENTE                             # test non isolé, sur l'exécuteur de CI
database._tls.__dict__.pop("conn", None)
try:
    database.db_set_setting("verif_garde_absente", "1")
    ok(database.db_get_setting("verif_garde_absente") == "1",
       "get_db : la base jetable a reçu le schéma (réglages lisibles et inscriptibles)")
except sqlite3.Error as e:
    ok(False, "get_db sur une production absente : %s" % e)
database._tls.__dict__.pop("conn", None)
mr.MEDIA_DB_PATH = os.path.join(os.path.dirname(ABSENTE), "db_medias.db")
mr._tls.__dict__.pop("conns", None)
try:
    mr.db()
    ok(True, "registre des médias sur une production absente : ouvert")
except sqlite3.Error as e:
    ok(False, "registre des médias sur une production absente : %s" % e)
mr._tls.__dict__.pop("conns", None)
ok(os.listdir(os.path.dirname(ABSENTE)) == [],
   "aucun fichier créé à l'emplacement de la production (ni base, ni db_medias, ni WAL)")
ok(not database.production_write_attempts, "aucune écriture comptée comme écriture en production")

print()
if ECHECS:
    print("ÉCHEC : %d assertion(s)" % len(ECHECS))
    sys.exit(1)
print("TOUT VERT")
