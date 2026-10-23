#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du DÉNOUEMENT des niveaux de tally (app/database.py:_migrer_niveaux_tally).
#
# CE QU'IL PROTÈGE. Le « pas de 3 » venait du mot de contrôle TSL 5.0 (LH/RH/TT) et avait fui dans
# le modèle : une production ne pouvait avoir que trois chaînes de destination. La migration
# aplatit les bases en niveaux nommés 1..N, UN PAR PORTEUR — pas un par champ TSL : une première
# version en créait trois, ce qui transposait le pas de 3 au lieu de s'en défaire. Le rouge et le
# vert sont deux ÉTATS d'un même niveau, pas deux niveaux. Quatre façons de se tromper, toutes
# silencieuses :
#   · re-créer un niveau par champ (le modèle repart avec la trame de TSL dedans) ;
#   · perdre un niveau (un signal cesse d'être tallyé, personne ne le voit avant l'antenne) ;
#   · déplacer un niveau (un rouge apparaît sur la mauvaise source — pire) ;
#   · rejouer la migration et tout renuméroter une seconde fois.
#
# ⚠ Ce banc travaille sur une COPIE de la base. Il n'écrit jamais dans la production.
#
#   $ ./venv/bin/python tools/verif_tally_niveaux.py
import json
import os
import shutil
import sqlite3
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


import app.database as db                                           # noqa: E402

print("Tally — dénouement des niveaux\n")

_tmp = tempfile.mkdtemp(prefix="tally-niveaux-")
_copie = os.path.join(_tmp, "essai.sqlite")
# ⚠ PAS `shutil.copy` : LA BASE EST EN WAL. Copier le seul fichier `.db` laisse dans le journal
# tout ce qui n'a pas encore été replié — relevé le 2026-09-01 : 68 lignes dans la copie contre
# 49 en base. Ce banc tournait donc sur un instantané PÉRIMÉ, et il pouvait très bien passer sur
# des données qui n'existent plus. L'API `backup` de SQLite, elle, est consciente du WAL.
_src = sqlite3.connect(db.DB_PATH)
con = sqlite3.connect(_copie)
_src.backup(con)
_src.close()

try:
    # État hérité, AVANT migration (la vraie base peut déjà l'avoir subie : on repart d'une copie
    # dont on retire la table pour rejouer depuis les bases, qui sont conservées exprès).
    # ⚠ ON RETIRE LE MARQUEUR : ce banc rejoue la migration DEPUIS L'ÉTAT HÉRITÉ, ce qu'une base
    # déjà marquée ne fait plus (et c'est bien le but du marqueur). Sans ça, la migration sort
    # aussitôt et rien de ce qui suit ne mesure quoi que ce soit.
    con.execute("DELETE FROM settings WHERE key='tally_denouement_fait'")
    con.execute("DROP TABLE IF EXISTS tally_levels")
    con.execute("""CREATE TABLE tally_levels (
        id INTEGER PRIMARY KEY, uuid TEXT NOT NULL DEFAULT '', nom TEXT NOT NULL DEFAULT '',
        num INTEGER NOT NULL DEFAULT 0, owner_kind TEXT, owner_id INTEGER)""")
    for c in ("level_id", "level_uuid"):
        try:
            con.execute("UPDATE tsl_connections SET %s=NULL" % c)
        except sqlite3.OperationalError:
            pass
    bases = sorted(
        [(int(r[0]), "connection") for r in con.execute(
            "SELECT tally_base FROM tsl_connections WHERE tally_base IS NOT NULL")]
        + [(int(r[0]), "project") for r in con.execute(
            "SELECT tally_base FROM projects WHERE tally_base IS NOT NULL")])
    controle("★ des bases héritées existent, sinon le banc ne prouve rien", bool(bases),
             "aucune base à migrer sur cette base de données")
    attendu = len(bases)

    db._migrer_niveaux_tally(con)
    con.commit()
    lignes = con.execute("SELECT id, nom, num, owner_kind, owner_id FROM tally_levels "
                         "ORDER BY id").fetchall()
    ids = [r[0] for r in lignes]

    controle("★★★ UN niveau par porteur : %d porteurs → %d niveaux" % (len(bases), attendu),
             len(ids) == attendu,
             "trois par porteur, c'est le pas de 3 de TSL transposé au lieu d'être dénoué ; moins "
             "d'un, c'est un signal qui cesse d'être tallyé et ça ne se voit qu'à l'antenne. "
             "Obtenu %d" % len(ids))
    doublons = con.execute("SELECT owner_kind, owner_id, COUNT(*) c FROM tally_levels "
                           "GROUP BY 1,2 HAVING c > 1").fetchall()
    controle("★★★ aucun porteur ne détient plusieurs niveaux au semis",
             not doublons,
             "un porteur = une chaîne de destination = un niveau, dont les états expriment le "
             "rouge, le vert et leur cumul. Obtenu %s" % doublons)
    controle("★★★ numérotation 1..N, sans trou ni doublon",
             ids == list(range(1, len(ids) + 1)),
             "c'est tout l'objet du dénouement : le pas de 3 disparaît")
    controle("★ chaque niveau porte un nom et un propriétaire",
             all(r[1] and r[3] in ("project", "connection") and r[4] for r in lignes),
             "un niveau anonyme est illisible dans le plan tally")

    # ── L'ORDRE est préservé : c'est ce qui conserve le sens ─────────────
    ordre_ancien = [b for b, _ in bases]
    ordre_neuf = [con.execute("SELECT MIN(id) FROM tally_levels WHERE owner_kind=? AND owner_id=?",
                              (g, o)).fetchone()[0]
                  for (g, o) in [(r[3], r[4]) for r in lignes]]
    controle("★★★ l'ordre des porteurs est conservé",
             ordre_neuf == sorted(ordre_neuf) and len(ordre_neuf) == len(ordre_ancien),
             "déplacer un niveau fait apparaître un rouge sur la MAUVAISE source — pire que d'en "
             "perdre un")

    # ── Chaque connexion a reçu SON niveau, un seul ──────────────────────
    mauvais = []
    for cid, base, lvl in con.execute(
            "SELECT id, tally_base, level_id FROM tsl_connections WHERE tally_base IS NOT NULL"):
        if not lvl:
            mauvais.append((cid, base, lvl))
    controle("★★ chaque connexion TSL héritée a reçu son niveau", not mauvais,
             "sans niveau, une connexion n'écrit RIEN : elle se tait au lieu d'échouer. "
             "Obtenu %s" % mauvais)

    # ── Les TROIS anciens numéros d'un porteur mènent au MÊME niveau ─────
    # C'est ce qui rend la migration non destructrice : une référence héritée visait un CHAMP,
    # elle désigne maintenant la CHAÎNE — quel que soit le champ qu'elle visait.
    #
    # ⚠ CE CONTRÔLE A ÉTÉ REFAIT. La première version re-dérivait la correspondance depuis la
    # base (porteur → son niveau), et vérifiait donc sa propre arithmétique : muter la table
    # `ancien_vers_neuf` de la migration ne la faisait pas broncher. On observe maintenant le seul
    # EFFET que cette table produise — la réécriture des paramètres de plugin.
    vmid_t = con.execute("SELECT vmid FROM containers LIMIT 1").fetchone()
    if vmid_t and bases:
        vmid_t = vmid_t[0]
        base_t, genre_t = bases[0]
        dc_orig = con.execute("SELECT deploy_config FROM containers WHERE vmid=?",
                              (vmid_t,)).fetchone()[0]
        dc_t = json.loads(dc_orig or "{}")
        dc_t.setdefault("params", {}).update(
            {"tally_level": base_t, "tally_level_a": base_t + 1, "tally_level_b": base_t + 2})
        con.execute("UPDATE containers SET deploy_config=? WHERE vmid=?",
                    (json.dumps(dc_t), vmid_t))
        con.execute("DELETE FROM tally_levels")
        con.execute("DELETE FROM settings WHERE key='tally_denouement_fait'")
        con.execute("UPDATE tsl_connections SET level_id=NULL")
        db._migrer_niveaux_tally(con)
        pt = json.loads(con.execute("SELECT deploy_config FROM containers WHERE vmid=?",
                                    (vmid_t,)).fetchone()[0])["params"]
        vus_t = [pt.get("tally_level"), pt.get("tally_level_a"), pt.get("tally_level_b")]
        oid_t = con.execute(
            "SELECT id FROM %s WHERE tally_base=?" % ("tsl_connections" if genre_t == "connection"
                                                      else "projects"), (base_t,)).fetchone()[0]
        attendu_t = con.execute("SELECT id FROM tally_levels WHERE owner_kind=? AND owner_id=?",
                                (genre_t, oid_t)).fetchone()[0]
        controle("★★★ les trois anciens numéros d'un porteur mènent au MÊME niveau",
                 vus_t == [[attendu_t]] * 3,
                 "une référence héritée pointant sur le champ TT doit désigner la même chaîne que "
                 "celle qui pointait sur LH — sinon un réglage sur deux se tait, sans erreur. "
                 "Attendu %r trois fois, obtenu %r" % ([attendu_t], vus_t))
        con.execute("UPDATE containers SET deploy_config=? WHERE vmid=?", (dc_orig, vmid_t))
        lignes = con.execute("SELECT id, nom, num, owner_kind, owner_id FROM tally_levels "
                             "ORDER BY id").fetchall()

    # ── Idempotence ──────────────────────────────────────────────────────
    db._migrer_niveaux_tally(con)
    con.commit()
    controle("★★★ rejouer la migration ne change RIEN",
             con.execute("SELECT id, nom, num, owner_kind, owner_id FROM tally_levels "
                         "ORDER BY id").fetchall() == lignes,
             "elle tourne à CHAQUE démarrage : une seconde renumérotation décalerait tout le parc")

    # ── Réécriture des paramètres de plugin ──────────────────────────────
    vmid = con.execute("SELECT vmid FROM containers LIMIT 1").fetchone()
    if vmid:
        vmid = vmid[0]
        dc = json.loads(con.execute("SELECT deploy_config FROM containers WHERE vmid=?",
                                    (vmid,)).fetchone()[0] or "{}")
        dc.setdefault("params", {}).update({"tally_level": 2, "tally_level_a": 999})
        con.execute("UPDATE containers SET deploy_config=? WHERE vmid=?", (json.dumps(dc), vmid))
        db._reecrire_niveaux_plugins(con, {2: 3})
        p = json.loads(con.execute("SELECT deploy_config FROM containers WHERE vmid=?",
                                   (vmid,)).fetchone()[0])["params"]
        controle("★★ un niveau de plugin devient une LISTE", p.get("tally_level") == [3],
                 "la sélection est un ENSEMBLE combiné en OU ; « un seul » n'est que le cas à un "
                 "élément (cf. TODO.md § TALLY) — obtenu %r" % p.get("tally_level"))
        controle("★★★ un niveau DISPARU devient une liste vide, pas un pointeur",
                 p.get("tally_level_a") == [],
                 "le garder ferait pointer un niveau qui appartient désormais à quelqu'un "
                 "d'autre : un rouge sur la mauvaise source")
        p2 = dict(p)
        db._reecrire_niveaux_plugins(con, {2: 3})
        controle("★ et la réécriture est idempotente aussi",
                 json.loads(con.execute("SELECT deploy_config FROM containers WHERE vmid=?",
                                        (vmid,)).fetchone()[0])["params"] == p2)

    # ── LA BASCULE SUR L'UUID, qui suit immédiatement au démarrage ───────
    db._migrer_identite_niveaux(con)
    con.commit()
    uu = con.execute("SELECT uuid FROM tally_levels ORDER BY num").fetchall()
    controle("★★★ chaque niveau a reçu un UUID, tous distincts",
             all(u[0] and "-" in u[0] for u in uu) and len({u[0] for u in uu}) == len(uu),
             "sans identité stable, réordonner redevient une migration. Obtenu %s" % uu[:2])
    controle("★★★ les connexions TSL citent l'UUID, plus le rowid",
             all(r[0] and "-" in r[0] for r in con.execute(
                 "SELECT level_uuid FROM tsl_connections WHERE level_id IS NOT NULL")),
             "une connexion restée sur le rowid n'écrit plus rien du jour où on réordonne")
    db._migrer_identite_niveaux(con)
    con.commit()
    controle("★★ la bascule est idempotente",
             [r[0] for r in con.execute("SELECT uuid FROM tally_levels ORDER BY num")]
             == [u[0] for u in uu],
             "elle tourne à CHAQUE démarrage : régénérer les UUID couperait toutes les "
             "configurations du site, en silence")

    # ── ⚠⚠ LA MIGRATION NE DOIT PLUS JAMAIS RE-SEMER ────────────────────
    # Elle commence par un `DELETE FROM tally_levels`. Sa garde d'idempotence déduisait
    # « déjà faite » d'une FORME DE DONNÉES — « aucun porteur n'a plus d'un niveau » — or une
    # production peut légitimement en avoir plusieurs : c'est la capacité qu'on a ouverte en
    # dénouant. Le jour où quelqu'un s'en servait, le démarrage suivant effaçait TOUS les
    # niveaux, renommages compris, et les remplaçait par ceux déduits des vieilles colonnes.
    # Signalé par l'utilisateur le 2026-09-01 : « quand on supprime un niveau, ça supprime tous
    # les niveaux » — ce n'était pas la suppression, c'était le redémarrage qui suivait.
    import uuid as _uuid
    _pid = con.execute("SELECT owner_id FROM tally_levels WHERE owner_kind='project' "
                       "LIMIT 1").fetchone()
    _pid = _pid[0] if _pid else (con.execute("SELECT id FROM projects LIMIT 1").fetchone() or [None])[0]
    if _pid:
        controle("★★★ la migration a POSÉ le marqueur d'idempotence",
                 con.execute("SELECT COUNT(*) FROM settings "
                             "WHERE key='tally_denouement_fait'").fetchone()[0] == 1,
                 "sans lui, la garde retombe sur une inférence — et une inférence se trompe dès "
                 "que le modèle est utilisé comme prévu")

        _avant = con.execute("SELECT COUNT(*) FROM tally_levels").fetchone()[0]
        _rang = con.execute("SELECT COALESCE(MAX(num),0) FROM tally_levels").fetchone()[0]
        con.execute("INSERT INTO tally_levels (uuid,nom,num,owner_kind,owner_id) "
                    "VALUES (?,?,?,?,?)", (str(_uuid.uuid4()), "TÉMOIN 2e chaîne",
                                           _rang + 1, "project", _pid))
        con.commit()
        db._migrer_niveaux_tally(con)
        con.commit()
        _apres = [r[0] for r in con.execute("SELECT nom FROM tally_levels ORDER BY num")]
        controle("★★★ une production à DEUX niveaux ne déclenche PAS un re-semis",
                 len(_apres) == _avant + 1 and "TÉMOIN 2e chaîne" in _apres,
                 "c'est une capacité annoncée, pas une anomalie. Attendu %d niveaux, obtenu %d : "
                 "%s" % (_avant + 1, len(_apres), _apres[:6]))

        # ⚠ ET SANS LE FILET DES UUID. Le contrôle précédent ne suffit pas : à ce stade les
        # niveaux ont un UUID, ce qui fait sortir la migration par une AUTRE branche. Une mutation
        # l'a montré — retirer la garde du marqueur ne changeait rien. On remet donc la base dans
        # l'état exact d'avant la bascule d'identité : c'est là que la garde de forme mordait.
        con.execute("UPDATE tally_levels SET uuid=''")
        con.execute("DELETE FROM settings WHERE key='tally_denouement_fait'")
        con.commit()
        _avant2 = [r[0] for r in con.execute("SELECT nom FROM tally_levels ORDER BY num")]
        db._migrer_niveaux_tally(con)
        con.commit()
        _apres2 = [r[0] for r in con.execute("SELECT nom FROM tally_levels ORDER BY num")]
        controle("★★★ ...même sans UUID et sans marqueur, RIEN n'est détruit",
                 _apres2 == _avant2
                 and con.execute("SELECT COUNT(*) FROM settings "
                                 "WHERE key='tally_denouement_fait'").fetchone()[0] == 1,
                 "c'est ICI que la garde par forme de données effaçait tout : elle voyait une "
                 "production à deux niveaux et re-semait depuis les vieilles colonnes, "
                 "renommages compris. Le re-semis a été RETIRÉ — des niveaux qui existent font "
                 "poser le marqueur, point. Avant %s, après %s" % (_avant2[:5], _apres2[:5]))
        _src_mig = open(os.path.join(RACINE, "app", "database.py"), encoding="utf-8").read()
        _corps = _src_mig[_src_mig.index("def _migrer_niveaux_tally"):
                          _src_mig.index("def _migrer_colonnes_libelles")]
        _lignes = [l for l in _corps.split("\n")
                   if "DELETE FROM tally_levels" in l and not l.strip().startswith("#")]
        controle("★★★ plus AUCUN chemin ne peut vider la table au démarrage", not _lignes,
                 "cette fonction tourne à chaque boot : y laisser un `DELETE` sans WHERE, c'est "
                 "garder une perte de données à un `if` de distance. Obtenu %s" % _lignes)

    # ── Plus personne ne lit tally_base ──────────────────────────────────
    src = []
    for dossier in ("services", "app"):
        for r, _d, fs in os.walk(os.path.join(RACINE, dossier)):
            if "__pycache__" in r:
                continue
            for f in fs:
                if f.endswith(".py"):
                    src.append(os.path.join(r, f))
    lecteurs = []
    for f in src:
        if f.endswith(os.path.join("app", "database.py")):
            continue          # la migration DOIT les lire : c'est sa matière première
        txt = open(f, encoding="utf-8", errors="ignore").read()
        for i, ligne in enumerate(txt.split("\n"), 1):
            if "tally_base" in ligne and not ligne.strip().startswith("#"):
                lecteurs.append("%s:%d" % (os.path.relpath(f, RACINE), i))
    controle("★★★ plus aucun code hors migration ne lit `tally_base`", not lecteurs,
             "les colonnes sont conservées comme filet, mais toute lecture résiduelle ferait "
             "coexister deux modèles — %s" % lecteurs[:5])
finally:
    con.close()
    shutil.rmtree(_tmp, ignore_errors=True)
    print("\n  copie de travail supprimée (la base de production n'a pas été touchée)")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
