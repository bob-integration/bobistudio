#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Composition: the compositor fabric's INTERNAL nodes stay out of the instance list.

★ WHY. `/api/plugins/instances` feeds the Composition list, and it hides the fabric's internal
containers (`bobi-fab-*`): those are multiviews the fabric materialises on its own, not walls a
user edits. The filter only knew the kind "shard" (a tile serving ONE parent wall). A tile SHARED
by several walls is stored with kind "shared" — and went straight through. Everywhere else
`compositor_fabric` handles the two kinds together (`in ("shard", "shared")`, 6 sites); this route
alone distinguished them.

Measured on the cluster on 2026-11-27, after the fabric rework of the 25th: 6 internal nodes, ALL
of them "shared", none a "shard". The mask was therefore hiding NOTHING, and the six `bobi-fab-*`
containers were listed among the real walls, interleaved by vmid (reported by the tester).

Second defect on the same route: `int(r["ref"])` sat inside a single `try` wrapping the whole
loop, so ONE row with a non-numeric `ref` emptied the mask and brought every internal node back.
That is the failure mode that hurts most — it is silent and it looks like the first defect.

What is checked:
  1. a "shard" node is hidden (already the case before the fix);
  2. a "shared" node is hidden (the defect);
  3. the assemblers (= the user's walls, vmid set) stay listed;
  4. a malformed `ref` skips its own row only — the rest of the mask holds.

Run:
  $ ./venv/bin/python tests/verif_composition_masque_tissu.py
"""
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
# Base jetable posée AVANT le premier accès : la connexion est mémorisée par thread, donc
# surcharger DB_PATH après coup n'isolerait plus rien. On NE touche PAS `PRODUCTION_DB_PATH`.
import tempfile as _tempfile_iso                                           # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                   # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from app import database as db                                             # noqa: E402
from app.auth import hash_password                                         # noqa: E402
import main as app_main                                                    # noqa: E402
from app import plugins                                                    # noqa: E402

app = app_main.app
app.config["TESTING"] = True

print("Composition — les nœuds internes du tissu restent masqués\n")

# N'importe quel type de plugin chargé fait l'affaire : le masque ne regarde pas le type.
TYPE = "multiview" if plugins.is_plugin("multiview") else next(iter(sorted(plugins.REGISTRY)), None)
if not TYPE:
    print("  ⊘ sauté — aucun plugin chargé dans cette copie")
    sys.exit(0)

MURS = [(1406, "multiview"), (1420, "multiview-3")]
INTERNES = [(1413, "bobi-fab-66c020845def0bd9", "shard"),
            (1414, "bobi-fab-113b5f33a73dc8e0", "shared")]

for vmid, hostname in MURS + [(v, h) for v, h, _ in INTERNES]:
    db.db_upsert_container(vmid, hostname, status="running")
    db.db_update_deploy_config(vmid, TYPE, {"plugin_version": "1"})

for vmid, hostname in MURS:
    db.db_fabric_upsert("asm:%d" % vmid, 1, vmid, hostname, "assembler", 1920, 1080)
for vmid, hostname, kind in INTERNES:
    db.db_fabric_upsert(hostname[len("bobi-fab-"):], 1, None, "shm_%d" % vmid, kind,
                        960, 540, ref=str(vmid))

db.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin")
cli = app.test_client()
with cli.session_transaction() as s:
    u = db.db_get_user("essai")
    s["user_id"], s["username"] = u["id"], u["username"]


def listes():
    r = cli.get("/api/plugins/instances?type=" + TYPE)
    assert r.status_code == 200, r.status_code
    return sorted(x["hostname"] for x in (r.get_json() or []))


noms = listes()
controle('un nœud "shard" est masqué', "bobi-fab-66c020845def0bd9" not in noms,
         "listé : %r" % (noms,))
controle('un nœud "shared" est masqué', "bobi-fab-113b5f33a73dc8e0" not in noms,
         "c'est LE défaut mesuré le 27/11 — listé : %r" % (noms,))
controle("les murs restent listés", noms == ["multiview", "multiview-3"],
         "obtenu : %r" % (noms,))

# ── Une ligne fautive ne doit emporter QUE sa ligne ────────────────────────────────────
# Le tissu écrit `ref` tantôt en vmid, tantôt en nom docker (cf. db_fabric_upsert) : un ref
# non numérique n'est pas une hypothèse d'école.
#
# ⚠ L'ORDRE DES LIGNES FAIT LE VERDICT. Avec l'ancien code (un seul `try` autour de toute la
# boucle), une ligne fautive AJOUTÉE À LA FIN ne perd que les lignes qui la suivent : les refs
# déjà vus sont dans l'ensemble, le masque tient, et le test passe en ne mesurant rien. C'est
# mesuré : placée en dernier, cette ligne laisse la garde VERTE sur le code fautif. Il faut donc
# que la ligne fautive soit lue EN PREMIER — on la pose, puis on réécrit les internes derrière
# elle (`fabric_node_alloc` est lu sans ORDER BY, donc dans l'ordre d'insertion), et on vérifie
# cet ordre avant de conclure.
db.db_fabric_upsert("sig_cassee", 1, None, "shm_x", "shared", 960, 540,
                    ref="bobi-fab-sig_cassee")
for vmid, hostname, kind in INTERNES:
    db.db_fabric_delete(hostname[len("bobi-fab-"):])
    db.db_fabric_upsert(hostname[len("bobi-fab-"):], 1, None, "shm_%d" % vmid, kind,
                        960, 540, ref=str(vmid))

lignes = [r["signature"] for r in db.db_fabric_all()]
internes_apres = [hostname[len("bobi-fab-"):] for _v, hostname, _k in INTERNES]
controle("la ligne fautive est bien lue avant les internes",
         all(lignes.index("sig_cassee") < lignes.index(sig) for sig in internes_apres),
         "sinon le contrôle suivant ne prouve rien — ordre lu : %r" % (lignes,))

noms = listes()
controle("un ref non numérique ne vide pas le masque",
         noms == ["multiview", "multiview-3"],
         "obtenu : %r — les internes sont revenus" % (noms,))

print("\n%d OK, %d échec(s)" % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
