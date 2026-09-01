#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du NOMBRE DE COLONNES DE LIBELLÉ.
#
# CE QU'IL PROTÈGE. Le produit offrait huit colonnes personnalisées d'office ; un site en utilise
# deux ou trois, et les cinq autres allongeaient chaque tableau et chaque sélecteur sans rien
# porter. Le nombre devient un réglage (2 par défaut, extensible à 8). Quatre façons de se
# tromper, toutes silencieuses :
#   · appliquer le défaut de 2 à une installation qui en REMPLIT six — quatre colonnes
#     disparaîtraient de ses tableaux sur une simple mise à jour, sans un mot ;
#   · EFFACER en réduisant, au lieu de masquer — un libellé perdu ne se retrouve pas ;
#   · figer les bornes d'affichage de la page (elles se calculaient depuis 8 en dur : les
#     colonnes TSL et IS-07 se seraient décalées) ;
#   · tronquer la liste de noms à l'enregistrement, ce qui anonymiserait les colonnes masquées.
#
# ⚠ CE BANC TOUCHE `label_cols_actives` et le RESTAURE dans un `finally`.
#
#   $ ./venv/bin/python tools/verif_labels_colonnes.py
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


from app.database import db_get_setting, db_set_setting, get_db                # noqa: E402
from services import tsl                                                       # noqa: E402

print("Libellés — deux colonnes par défaut, extensibles\n")

AVANT = db_get_setting("label_cols_actives")
AVANT_NOMS = db_get_setting("tsl_label_names")
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

    # ── Ce que le serveur OFFRE ──────────────────────────────────────────
    db_set_setting("label_cols_actives", 2)
    noms = cli.get("/api/tsl/label_names").get_json()
    controle("★★★ deux colonnes personnalisées, plus hostname et MXL",
             len(noms) == 4 and noms[0] == "Hostname" and noms[1] == "MXL",
             "obtenu %r" % noms)
    controle("★★ le nombre s'augmente jusqu'à 8",
             cli.post("/api/tsl/label_cols", json={"actives": 6}).status_code == 200
             and len(cli.get("/api/tsl/label_names").get_json()) == 8)
    controle("★★ au-delà de 8, refusé",
             cli.post("/api/tsl/label_cols", json={"actives": 9}).status_code == 400,
             "huit colonnes physiques existent : en offrir neuf donnerait une colonne qui ne "
             "s'enregistre nulle part")
    controle("★ et en-dessous de 1 aussi",
             cli.post("/api/tsl/label_cols", json={"actives": 0}).status_code == 400)

    # ── RÉDUIRE MASQUE, N'EFFACE PAS ─────────────────────────────────────
    db_set_setting("label_cols_actives", 6)
    avant_noms = tsl.noms_colonnes()
    db_set_setting("label_cols_actives", 2)
    controle("★★★ réduire ne touche pas aux NOMS des colonnes masquées",
             tsl.noms_colonnes() == avant_noms,
             "les rouvrir doit les retrouver nommées, pas anonymes")
    with get_db() as db:
        remplies = {i: db.execute("SELECT COUNT(*) FROM source_labels WHERE label_%d <> ''" % i)
                    .fetchone()[0] for i in range(2, 10)}
    db_set_setting("label_cols_actives", 6)
    with get_db() as db:
        apres = {i: db.execute("SELECT COUNT(*) FROM source_labels WHERE label_%d <> ''" % i)
                 .fetchone()[0] for i in range(2, 10)}
    controle("★★★ et aucun LIBELLÉ n'est effacé au passage", remplies == apres,
             "réduire l'affichage doit masquer, pas détruire — un libellé perdu ne se retrouve "
             "pas. Avant %r, après %r" % (remplies, apres))

    # ── L'enregistrement des noms ne tronque pas ─────────────────────────
    # ⚠ ON RENOMME D'ABORD UNE COLONNE HAUTE. Sans ça le contrôle ne distingue rien : les noms
    # au-delà des offertes valent leur défaut, et une troncature les rend… identiques au défaut.
    # Une mutation l'a montré — le banc acquiesçait à sa propre indifférence.
    db_set_setting("label_cols_actives", 8)
    cli.post("/api/tsl/label_names",
             json=["Hostname", "MXL", "L2", "L3", "L4", "L5", "L6", "TÉMOIN-7", "L8", "L9"])
    avant_noms = tsl.noms_colonnes()
    db_set_setting("label_cols_actives", 2)
    r = cli.post("/api/tsl/label_names", json=["Hostname", "MXL", "Antenne", "Plateau"])
    controle("★★ on peut enregistrer une liste TRONQUÉE (celle qu'on affiche)",
             r.status_code == 200, "obtenu %s" % r.status_code)
    controle("★★★ ...sans anonymiser les colonnes masquées",
             tsl.noms_colonnes()[4:] == avant_noms[4:] and "TÉMOIN-7" in tsl.noms_colonnes(),
             "enregistrer la liste visible telle quelle écraserait le nom des autres, qu'on "
             "retrouverait « Label 6 » en les rouvrant. Obtenu %r" % tsl.noms_colonnes()[4:])

    # ── Les macros ne proposent que ce qui est offert ────────────────────
    opts = tsl.action_options("set_label", "col")
    controle("★★ une macro ne propose que les colonnes offertes", len(opts) == 2,
             "écrire dans une colonne que personne n'affiche est une action sans effet "
             "visible. Obtenu %r" % opts)

    # ── LA PAGE : les bornes d'affichage SUIVENT ─────────────────────────
    html = cli.get("/labels").data.decode("utf-8", "replace")
    bloc = next((b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
                 if "function colKey" in b), None)
    controle("★ le modèle de colonnes est servi", bloc is not None)
    if bloc:
        js = ("""
const DOM={}; const EL=()=>({innerHTML:'',value:'',style:{},dataset:{},
  classList:{add(){},remove(){},contains:()=>false},querySelector:()=>EL(),
  querySelectorAll:()=>[],addEventListener:()=>{},closest:()=>EL()});
global.window={t:(k,d)=>d||k,addEventListener:()=>{},MXLPoll:()=>0,
  localStorage:{getItem:()=>null,setItem:()=>{}},SourceLabels:{refresh(){}}};
global.document={getElementById:(i)=>(DOM[i]=DOM[i]||EL()),querySelector:()=>null,
  querySelectorAll:()=>[],addEventListener:()=>{},createElement:()=>EL()};
global.fetch=async()=>({ok:false,json:async()=>({})}); global.mxlToast=()=>{};
global.setInterval=()=>0; global.setTimeout=()=>0;
""" + bloc + """
_tslConns = [{id:1,name:'a'}]; _i7Conns = []; _tslNiveaux = [];
const out = {mesures: []};
try {
  for (const n of [2, 4, 8]) {
    _names = ['Hostname','MXL'].concat(Array.from({length:n},(_,i)=>'L'+i));
    TOTAL_COLS = tslColStart() + _tslConns.length + _i7Conns.length;
    out.mesures.push([n, nbLabelsPerso(), colLabelEnd(), tslColStart(),
                      colKey(colLabelEnd()), colKey(tslColStart())]);
  }
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
process.exit(0);
""")
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        f.write(js)
        f.close()
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
            ligne = (r.stdout or "").strip().split("\n")[-1]
            res = json.loads(ligne) if ligne.startswith("{") else {"exception": (r.stderr or "")[:200]}
        finally:
            os.unlink(f.name)
        controle("★★★ les bornes d'affichage suivent le nombre offert",
                 res.get("mesures") == [[2, 2, 6, 7, "label_3", "tsl_1"],
                                        [4, 4, 8, 9, "label_5", "tsl_1"],
                                        [8, 8, 12, 13, "label_9", "tsl_1"]],
                 "elles se calculaient depuis 8 en dur : les colonnes TSL et IS-07 se seraient "
                 "décalées, et on aurait écrit un index dans une colonne de libellé. "
                 "Obtenu %r" % (res.get("mesures") or res.get("exception")))
finally:
    db_set_setting("label_cols_actives", AVANT if AVANT else 2)
    if AVANT_NOMS:
        db_set_setting("tsl_label_names", AVANT_NOMS)
    print("\n  restauré : actives=%r · noms=%r"
          % (db_get_setting("label_cols_actives"), db_get_setting("tsl_label_names")))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
