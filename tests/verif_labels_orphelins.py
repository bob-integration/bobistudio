#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des SOURCES ABSENTES dans la table des libellés.
#
# CE QU'IL PROTÈGE. Sur ce parc, 46 lignes sur 68 portaient un flux qu'aucun conteneur ne déclare
# plus — deux tiers de la table — et rien ne le disait. Quatre façons de mal traiter ça, et trois
# d'entre elles font perdre du travail :
#   · confondre « absent de la DÉCLARATION » et « éteint ». `/api/sources` dérive de
#     `deploy_config` : un conteneur ARRÊTÉ déclare toujours ses flux. Si on se basait sur l'état
#     d'exécution, arrêter un conteneur ferait basculer tous ses libellés en « à nettoyer », et
#     quelqu'un les supprimerait ;
#   · supprimer tout seul CE QUI EST CONFIGURÉ — un libellé écrit, une correspondance TSL ou
#     IS-07, c'est un réglage qui attend que le conteneur revienne ;
#   · à l'inverse, garder indéfiniment ce qui n'a AUCUNE configuration : ça ne porte le travail
#     de personne, et sur ce parc c'était dix-neuf lignes sur soixante-huit ;
#   · masquer la ligne au lieu de la marquer : on chercherait longtemps pourquoi un libellé a
#     disparu ;
#   · retirer une ligne qu'une correspondance TSL ou IS-07 vise encore, ce qui casse un tally
#     en silence.
#
# ⚠ CE BANC CRÉE une ligne de libellé de test et la RETIRE dans un `finally`.
#
#   $ ./venv/bin/python tools/verif_labels_orphelins.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from app.database import (db_upsert_source_label, db_delete_source_label,          # noqa: E402
                          db_get_containers, db_upsert_is07_connection,
                          db_delete_is07_connection, db_set_is07_mapping_for_source,
                          db_get_tally_levels, get_db)

print("Libellés — les sources qui n'existent plus\n")

FANTOME = "banc-orphelin-inexistant_0"
VIVANT = None
cid = None
try:
    import main                                                                # noqa: E402
    app = main.app
    app.config["TESTING"] = True
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]

    srcs = cli.get("/api/sources").get_json() or []
    VIVANT = next((s["shm"] for s in srcs if s.get("shm")), None)
    controle("★ il existe au moins une source déclarée", bool(VIVANT))

    db_upsert_source_label(FANTOME, {"label_2": "TÉMOIN"})
    orph = {x["shm"]: x for x in cli.get("/api/source_labels/orphelins").get_json()}
    controle("★★★ une ligne sans producteur est signalée absente", FANTOME in orph,
             "sans marque, elle se mêle aux sources vivantes — deux tiers de la table sur ce "
             "parc, et rien ne le disait")
    controle("★★★ une source DÉCLARÉE ne l'est jamais", VIVANT not in orph,
             "obtenu %r" % list(orph)[:3])
    controle("★★ et on dit qu'elle porte un libellé",
             orph.get(FANTOME, {}).get("rempli") == ["label_2"],
             "vide ou renseignée n'appellent pas la même décision : l'une ne coûte qu'une "
             "ligne, l'autre est du travail. Obtenu %r" % orph.get(FANTOME))

    # ── ARRÊTÉ N'EST PAS ABSENT — la propriété qui rend tout le reste sûr ─
    # ⚠ ON NE PEUT PAS LA MESURER EN ARRÊTANT UN CONTENEUR : la boucle de surveillance en
    # redémarrerait un, et un banc n'a pas à toucher à la flotte. On la prouve donc en deux
    # temps, et il faut les deux — le premier dit que la règle est tenue sur les données du
    # jour, le second qu'elle l'est PAR CONSTRUCTION.
    arretes = [c for c in db_get_containers() if (c.get("status") or "") != "running"]
    declarees = {s["shm"] for s in srcs if s.get("shm")}
    controle("★★★ aucune source déclarée n'est comptée absente, quel que soit son état",
             not (declarees & set(orph)),
             "si « absent » voulait dire « éteint », arrêter un conteneur ferait basculer tous "
             "ses libellés en « à nettoyer » et quelqu'un les supprimerait. %d conteneur(s) non "
             "running dans la flotte. Intersection : %r"
             % (len(arretes), list(declarees & set(orph))[:3]))
    src_route = open(os.path.join(RACINE, "services", "tsl", "__init__.py"),
                     encoding="utf-8").read()
    i = src_route.index("def source_labels_orphelins()")
    corps = src_route[i:src_route.index("def source_labels_orphelins_purge()")]
    controle("★★★ ...et la détection ne CONSULTE JAMAIS l'état d'exécution",
             "deploy_config" in corps and "status" not in corps and "running" not in corps,
             "c'est la garantie structurelle : la liste des flux déclarés se dérive de "
             "`deploy_config`, que l'arrêt d'un conteneur ne change pas. Lire `status` ici "
             "suffirait à rendre la fonction destructrice")

    # ── ON NE SUPPRIME QUE CE QU'ON DEMANDE ──────────────────────────────
    n_avant = len(cli.get("/api/source_labels").get_json() or [])
    cli.get("/api/source_labels/orphelins")
    controle("★★★ CONSULTER ne supprime rien",
             len(cli.get("/api/source_labels").get_json() or []) == n_avant,
             "une purge au passage ferait disparaître du travail sur un simple affichage")

    # ── Une ligne VISÉE par une correspondance est protégée ──────────────
    niv = db_get_tally_levels()
    if niv:
        cid = db_upsert_is07_connection({"name": "banc orph", "enabled": 0,
                                         "level_uuid": niv[0]["uuid"]})
        db_set_is07_mapping_for_source(cid, FANTOME, "11111111-2222-3333-4444-555555555555")
        orph = {x["shm"]: x for x in cli.get("/api/source_labels/orphelins").get_json()}
        controle("★★ on signale qu'une correspondance la vise encore",
                 orph.get(FANTOME, {}).get("mappe") is True, "obtenu %r" % orph.get(FANTOME))
        r = cli.post("/api/source_labels/orphelins", json={"shms": [FANTOME]})
        j = r.get_json() or {}
        controle("★★★ et on REFUSE de la retirer",
                 j.get("retires") == 0 and FANTOME in (j.get("refuses") or []),
                 "le tally l'adresse : l'effacer casserait quelque chose en silence, même si le "
                 "flux a disparu. Obtenu %r" % j)
        controle("★★ la ligne est toujours là",
                 any(l.get("shm") == FANTOME
                     for l in (cli.get("/api/source_labels").get_json() or [])))
        db_set_is07_mapping_for_source(cid, FANTOME, "")
        db_delete_is07_connection(cid)
        cid = None

    # ── Une fois libérée, elle se retire ─────────────────────────────────
    j = cli.post("/api/source_labels/orphelins", json={"shms": [FANTOME]}).get_json() or {}
    controle("★★★ une orpheline non visée se retire quand on le demande",
             j.get("retires") == 1 and not (j.get("refuses") or []), "obtenu %r" % j)

    # ── LE BALAYAGE AUTOMATIQUE : ce qui n'a AUCUNE config s'en va seul ──
    from app.database import db_purger_libelles_orphelins, db_get_source_labels
    db_upsert_source_label(FANTOME, {"label_2": "TÉMOIN"})
    VIDE = "banc-orphelin-vide_0"
    db_upsert_source_label(VIDE, {"label_2": ""})
    n = db_purger_libelles_orphelins()
    restants = {l["shm"] for l in db_get_source_labels()}
    controle("★★★ une orpheline SANS configuration est balayée automatiquement",
             VIDE not in restants and n >= 1,
             "elle ne porte le travail de personne et il n'y a rien à décider — la garder "
             "encombre la table pour rien. Retirées : %d" % n)
    controle("★★★ une orpheline CONFIGURÉE est ÉPARGNÉE par le balayage",
             FANTOME in restants,
             "un libellé écrit à la main est un réglage qui attend que le conteneur revienne : "
             "le balayer serait perdre le travail de quelqu'un sur un simple redémarrage")
    controle("★★ une source DÉCLARÉE n'est jamais balayée, même sans libellé",
             VIDE not in restants and VIVANT in {l["shm"] for l in db_get_source_labels()}
             or VIVANT not in restants,
             "le balayage ne regarde que ce qui n'est plus produit")
    controle("★★ le balayage est idempotent", db_purger_libelles_orphelins() == 0,
             "il tourne à CHAQUE démarrage : un second passage ne doit plus rien trouver")

    # ── LES DEUX GARDES QUI NE SE TESTENT QUE SUR UNE COPIE ──────────────
    # ⚠ CETTE FONCTION SUPPRIME. Muter son code pour vérifier ses gardes exécuterait de VRAIES
    # suppressions sur la vraie base — c'est arrivé le 2026-09-01, deux fois, et il a fallu
    # restaurer les libellés. On travaille donc sur une copie, en injectant la connexion.
    #
    # ⚠ ET LA COPIE PASSE PAR `backup()`, PAS PAR `shutil.copy` : la base est en WAL, et copier
    # le seul fichier `.db` laisse dans le journal tout ce qui n'a pas été replié — 68 lignes
    # dans la copie contre 49 en base, mesuré le même jour.
    import shutil                                                       # noqa: F401
    import sqlite3
    import tempfile
    d = tempfile.mkdtemp(prefix="labels-orph-")
    try:
        chemin = os.path.join(d, "essai.sqlite")
        _s = sqlite3.connect(sys.modules["app.database"].DB_PATH)
        _c = sqlite3.connect(chemin)
        _c.row_factory = sqlite3.Row
        _s.backup(_c)
        _s.close()
        controle("★ la copie reflète bien la base (WAL compris)",
                 _c.execute("SELECT COUNT(*) FROM source_labels").fetchone()[0]
                 == len(db_get_source_labels()),
                 "une copie périmée ferait passer ce banc sur des données qui n'existent plus")

        # Une orpheline VIDE mais VISÉE par une correspondance doit survivre.
        _c.execute("INSERT OR REPLACE INTO source_labels (shm) VALUES (?)", ("orph-mappee_0",))
        _c.execute("INSERT OR REPLACE INTO is07_mapping (connection_id, source_id, source_shm) "
                   "VALUES (?,?,?)", (999, "s-1", "orph-mappee_0"))
        # Une orpheline VIDE et non visée doit partir.
        _c.execute("INSERT OR REPLACE INTO source_labels (shm) VALUES (?)", ("orph-nue_0",))
        _c.commit()
        db_purger_libelles_orphelins(_c)
        restants2 = {r[0] for r in _c.execute("SELECT shm FROM source_labels")}
        controle("★★★ une ligne visée par une correspondance survit au balayage, même VIDE",
                 "orph-mappee_0" in restants2,
                 "le tally l'adresse : la balayer casserait quelque chose en silence, et le fait "
                 "qu'elle soit vide n'y change rien")
        controle("★★ ...alors que la même sans correspondance s'en va",
                 "orph-nue_0" not in restants2,
                 "sinon le contrôle précédent ne prouverait rien")
        _c.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    controle("★★ ...et une source VIVANTE ne peut pas être retirée par cette route",
             VIVANT not in {x["shm"] for x in cli.get("/api/source_labels/orphelins").get_json()},
             "elle n'y figure pas, donc l'interface ne peut pas la proposer")
finally:
    try:
        if cid:
            db_set_is07_mapping_for_source(cid, FANTOME, "")
            db_delete_is07_connection(cid)
    except Exception:
        pass
    db_delete_source_label(FANTOME)
    try:
        db_delete_source_label("banc-orphelin-vide_0")
    except Exception:
        pass
    print("\n  lignes de banc retirées")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
