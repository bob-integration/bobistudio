#!/usr/bin/env python3
"""Recherche de la base des médias : un critère NUMÉRIQUE se compare à ce que l'interface AFFICHE.

Recette du 2026-10-12 : « durée = 8 » → « rien ne correspond », alors qu'un clip de 8,44 s s'affiche
« 0:08 ». La durée est stockée en secondes flottantes ; l'égalité stricte ne trouvait jamais rien.
Garde : durée comparée à la seconde (arrondie comme l'affichage), saisie « m:ss » acceptée,
cadence au centième ; un champ entier reste exact."""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_nb_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()
from app import media_registry as mr                                    # noqa: E402

mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")

ok = fail = 0


def check(cond, msg):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("  ✓ " if cond else "  ✗ ") + msg)


c = mr.db()
for aid, name, dur, fps, w in (("a1", "court", 8.441333, 25.0, 1920), ("a2", "rond", 8.62, 29.97002997, 1280),
                               ("a3", "dix", 10.0, 50.0, 1920), ("a4", "sept", 7.2, 25.0, 720)):
    c.execute("INSERT INTO media_assets(id, name, tech_json, created_at, updated_at) VALUES (?,?,?,?,?)",
              (aid, name, json.dumps({"duration": dur, "video": {"fps": fps, "width": w}}), "2026-10-12T10:00:00", "2026-10-12T10:00:00"))
c.commit()


def names(*conds):
    r = mr.search({"all": list(conds)})
    rows = r if isinstance(r, list) else r.get("assets", r)
    return sorted(a["name"] for a in rows)


def cond(field, op, value):
    return {"field": field, "op": op, "value": value}


check(names(cond("duration", "eq", 8)) == ["court"], "durée = 8 trouve le clip de 8,44 s (affiché 0:08)")
check(names(cond("duration", "eq", "0:08")) == ["court"], "durée = « 0:08 » (saisie comme affichée)")
check(names(cond("duration", "eq", "0:09")) == ["rond"], "8,62 s s'affiche 0:09 et se trouve à 9")
check(names(cond("duration", "ne", 8)) == ["dix", "rond", "sept"], "durée ≠ 8 exclut celui-là seulement")
check(names(cond("duration", "gt", "0:08"), cond("duration", "lte", 10)) == ["dix", "rond"], "8 < durée ≤ 10, à la seconde")
check(names(cond("duration", "eq", "8,0")) == ["court"], "virgule décimale acceptée")
check(names(cond("fps", "eq", 29.97)) == ["rond"], "cadence = 29.97 trouve 29.97002997 (au centième)")
check(names(cond("width", "eq", 1920)) == ["court", "dix"], "un champ entier reste exact")
try:
    mr.search({"all": [cond("duration", "eq", "huit")]})
    check(False, "une valeur non numérique est refusée")
except ValueError:
    check(True, "une valeur non numérique est refusée")

print("\n%d ok, %d en échec" % (ok, fail))
sys.exit(1 if fail else 0)
