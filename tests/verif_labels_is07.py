#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de la COLONNE IS-07 dans la page Labels.
#
# POURQUOI ICI ET PAS AILLEURS. Une connexion IS-07 entrante est le même objet qu'une connexion
# TSL — elle écrit des tally dans UN niveau — et la question posée par cette page est la même :
# « quelle adresse de l'émetteur désigne quel signal ». Chez TSL l'adresse est un index de trame,
# en IS-07 l'UUID d'une Source. Deux tables séparées auraient obligé un site qui reçoit des deux
# protocoles à tenir la même liste de signaux à deux endroits.
#
# CE QU'IL PROTÈGE, et qui casse en silence :
#   · le début des colonnes IS-07 DÉPEND du nombre de connexions TSL — une constante s'y serait
#     décalée à la première connexion TSL ajoutée, et on aurait écrit un UUID dans une colonne
#     d'index ;
#   · une colonne non déclarée éditable ne s'enregistre pas, sans message ;
#   · `node --check` ne voit rien de tout ça : seule l'EXÉCUTION du modèle de colonnes le montre.
#
# ⚠ CE BANC CRÉE une connexion IS-07 de test et la SUPPRIME dans un `finally`.
#
#   $ ./venv/bin/python tools/verif_labels_is07.py
import json
import os
import re
import subprocess
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


PRELUDE = """
const DOM={}; const EL=()=>({innerHTML:'',value:'',style:{},dataset:{},
  classList:{add(){},remove(){},contains:()=>false},querySelector:()=>EL(),
  querySelectorAll:()=>[],addEventListener:()=>{},closest:()=>EL()});
global.window={t:(k,d)=>d||k,addEventListener:()=>{},MXLPoll:()=>0,
  localStorage:{getItem:()=>null,setItem:()=>{}},SourceLabels:{refresh(){}}};
global.document={getElementById:(i)=>(DOM[i]=DOM[i]||EL()),querySelector:()=>null,
  querySelectorAll:()=>[],addEventListener:()=>{},createElement:()=>EL()};
global.fetch=async()=>({ok:false,json:async()=>({})}); global.mxlToast=()=>{};
global.setInterval=()=>0; global.setTimeout=()=>0;
"""

from app.database import (db_get_tally_levels, db_upsert_is07_connection,          # noqa: E402
                          db_delete_is07_connection, db_set_is07_mapping_for_source,
                          db_get_is07_mappings_all, get_db)

print("Page Labels — la colonne d'une connexion IS-07 entrante\n")

niveaux = db_get_tally_levels()
if not niveaux:
    raise SystemExit("aucun niveau de tally : le banc ne prouverait rien")
cid = db_upsert_is07_connection({"name": "banc IS-07", "enabled": 1,
                                 "level_uuid": niveaux[0]["uuid"]})
try:
    import main                                                        # noqa: E402
    app = main.app
    app.config["TESTING"] = True
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]

    tsl = cli.get("/api/tsl/connections").get_json() or []
    i7 = (cli.get("/api/tally/is07/connections").get_json() or {}).get("connections") or []
    controle("★ la connexion de banc est servie par l'API",
             any(c["id"] == cid for c in i7), "obtenu %r" % i7)

    html = cli.get("/labels").data.decode("utf-8", "replace")
    bloc = next((b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
                 if "function colKey" in b), None)
    controle("★★ le modèle de colonnes est servi par la page", bloc is not None)
    if not bloc:
        raise SystemExit(1)

    noms = cli.get("/api/tsl/label_names").get_json() or []
    epi = ("\n_tslConns = " + json.dumps(tsl) + "; _i7Conns = " + json.dumps(i7)
           + "; _tslNiveaux = []; _names = " + json.dumps(noms) + ";\n"
             "TOTAL_COLS = tslColStart() + _tslConns.length + _i7Conns.length;\n" + """
const out = {};
try {
  out.total = TOTAL_COLS;
  out.debut = i7ColStart();
  out.base = tslColStart();
  out.cles = []; out.familles = [];
  for (let c = tslColStart(); c < TOTAL_COLS; c++) {
    out.cles.push(colKey(c));
    out.familles.push(isI7Col(c) ? 'is07' : (isTslCol(c) ? 'tsl' : '?'));
  }
  out.ligneVide = Object.keys(emptyRow('x','','',false)).filter(k => k.startsWith('is07_'));
  out.editables = _editableCols();
  // ⚠ ON INTERROGE `colEditable` DIRECTEMENT. Une première version regardait `_editableCols()`,
  // qui construit sa liste depuis `_i7Conns` sans passer par `colEditable` : retirer `isI7Col`
  // de celui-ci ne faisait pas broncher le banc, alors que c'est LUI que le rendu consulte.
  out.editableDirect = [];
  for (let c = i7ColStart(); c < TOTAL_COLS; c++) out.editableDirect.push(colEditable(c));
  // ⚠ ET ON FAIT VARIER LE NOMBRE DE CONNEXIONS TSL. Avec les deux connexions du banc,
  // `13 + 2 === 15` : un début figé à 15 passait inaperçu. On en simule d'autres.
  const vrai = _tslConns;
  out.debutVariable = [];
  for (const n of [0, 1, 5]) {
    _tslConns = Array.from({length: n}, (_, i) => ({id: 900 + i, name: 'x'}));
    out.debutVariable.push([n, i7ColStart() - tslColStart()]);   // l'ÉCART, pas l'absolu
  }
  _tslConns = vrai;
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
process.exit(0);
""")
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(PRELUDE + bloc + epi)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        sortie = (r.stdout or "").strip().split("\n")[-1]
        res = json.loads(sortie) if sortie.startswith("{") else {"exception": (r.stderr or "")[:200]}
    finally:
        os.unlink(f.name)

    controle("★★★ le modèle de colonnes s'exécute", not res.get("exception"),
             "`node --check` ne voit pas une référence morte : seule l'exécution la montre. %r"
             % res.get("exception"))

    n_tsl, n_i7 = len(tsl), len(i7)
    base = res.get("base")
    # On mesure l'ÉCART au début des colonnes TSL, pas une position absolue : celle-ci dépend
    # aussi du nombre de colonnes de libellé, qui est devenu un réglage. Un banc qui la fige
    # casserait au premier « + Colonne » sans rien signaler d'utile.
    controle("★★★ le début des colonnes IS-07 SUIT le nombre de connexions TSL",
             res.get("debutVariable") == [[0, 0], [1, 1], [5, 5]],
             "une constante s'y serait décalée à la première connexion TSL ajoutée, et on aurait "
             "écrit un UUID de Source dans une colonne d'index. On fait VARIER le nombre de "
             "connexions : figé, il passait inaperçu sur les deux du banc. Obtenu %r"
             % res.get("debutVariable"))
    controle("★★ une colonne par connexion, des deux protocoles",
             base is not None and res.get("total") == base + n_tsl + n_i7
             and res.get("familles") == ["tsl"] * n_tsl + ["is07"] * n_i7,
             "obtenu total=%r familles=%r" % (res.get("total"), res.get("familles")))
    controle("★★★ la clé d'une colonne IS-07 porte l'id de SA connexion",
             ("is07_%d" % cid) in (res.get("cles") or []),
             "deux connexions partageant une clé écriraient l'une sur l'autre. Obtenu %r"
             % res.get("cles"))
    controle("★★★ `colEditable` la reconnaît — c'est LUI que le rendu consulte",
             res.get("editableDirect") == [True] * n_i7,
             "une colonne non éditable ne s'enregistre pas, et rien ne le dit. `_editableCols()` "
             "ne suffit pas à le prouver : il construit sa liste sans passer par `colEditable`. "
             "Obtenu %r" % res.get("editableDirect"))
    controle("★★ ...et elle figure aussi dans la liste des colonnes éditables",
             res.get("debut") is not None
             and all(c in (res.get("editables") or [])
                     for c in range(res["debut"], res["debut"] + n_i7)),
             "obtenu %r" % res.get("editables"))
    controle("★★ une ligne vide porte déjà la clé",
             res.get("ligneVide") == ["is07_%d" % cid],
             "sans elle, la première saisie crée un champ que la sérialisation ignore. Obtenu %r"
             % res.get("ligneVide"))

    # ── Aller-retour de la correspondance ────────────────────────────────
    shm = "banc-is07-flux"
    db_set_is07_mapping_for_source(cid, shm, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    m = [x for x in db_get_is07_mappings_all() if x["connection_id"] == cid]
    controle("★★ la correspondance s'enregistre et se relit",
             len(m) == 1 and m[0]["source_shm"] == shm, "obtenu %r" % m)
    db_set_is07_mapping_for_source(cid, shm, "11111111-2222-3333-4444-555555555555")
    m = [x for x in db_get_is07_mappings_all() if x["connection_id"] == cid]
    controle("★★★ réaffecter REMPLACE, sans laisser l'ancienne Source",
             len(m) == 1 and m[0]["source_id"].startswith("11111111"),
             "deux Sources sur le même signal, et la périmée garderait un tally que plus rien "
             "ne met à jour. Obtenu %r" % m)
    db_set_is07_mapping_for_source(cid, shm, "")
    controle("★★ vider la cellule retire la correspondance",
             not [x for x in db_get_is07_mappings_all() if x["connection_id"] == cid])
finally:
    db_delete_is07_connection(cid)
    restant = [x for x in db_get_is07_mappings_all() if x["connection_id"] == cid]
    print("\n  connexion de banc supprimée · correspondances résiduelles : %d" % len(restant))
    if restant:
        echecs.append("correspondances non nettoyées")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
