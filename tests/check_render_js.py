#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# check_render_js.py — garde-fou CI des fonctions de rendu JS des pages.
#
# POURQUOI CET OUTIL EXISTE
#
# Les pages du produit construisent leur HTML par concaténation de chaînes dans de longues
# fonctions `renderX(...)`. Ces fonctions ne tournent QUE dans le navigateur, sur des données
# réelles : rien, dans le dépôt, ne les exécute jamais. Une variable supprimée mais encore
# interpolée passe donc tous les contrôles — `node --check` valide la SYNTAXE et ne peut pas voir
# une référence non résolue, qui n'existe qu'à l'exécution. Constaté : `unfedCls` retiré de la
# page Câbles, gabarit inchangé, `ReferenceError` au premier port rendu et toute la topologie
# perdue. Une page qui se compile n'est pas une page qui marche.
#
# CE QU'IL FAIT
#
# 1. Rend la page via le client de test Flask (donc le HTML RÉELLEMENT servi, Jinja appliqué).
# 2. Extrait les fonctions demandées de ses scripts inline.
# 3. Les EXÉCUTE dans node sur une matrice d'arguments couvrant les cas de bord.
#
# BOUCHONNAGE AUTOMATIQUE : une fonction de rendu s'appuie sur des dizaines de globales de page
# (helpers, catalogues, état). Les énumérer à la main serait faux dès la première évolution. On
# exécute donc, on lit le `X is not defined` renvoyé par node, on ajoute un bouchon pour X, et on
# recommence jusqu'au point fixe. Les globales ainsi découvertes sont AFFICHÉES : c'est la liste
# des dépendances réelles de la fonction, utile en soi.
#
# LIMITE ASSUMÉE : un bouchon rend une valeur neutre, donc l'outil prouve « ça s'exécute sans
# référence morte sur toutes ces formes d'entrée », pas « ça affiche la bonne chose ». C'est
# exactement la classe de bug qu'on a payée, et il ne prétend à rien de plus.
#
# Usage : ./venv/bin/python tools/check_render_js.py
#         ./venv/bin/python tools/check_render_js.py --page /cables --fn renderPort

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Fonctions de rendu couvertes, par page. Une fonction absente de la page rendue est signalée
# (elle a été renommée ou supprimée) plutôt qu'ignorée en silence.
CIBLES = {
    "/cables": ["fmtSummary", "renderPort", "renderPlaceholderInput", "renderNode"],
    # ⚠ PAS /settings ICI. Essayé le 2026-09-01, RETIRÉ le jour même : `extraire_fonction`
    # découpe par comptage d'accolades, sans comprendre les gabarits. Sur une fonction dont un
    # gabarit est cassé par un accent grave, il tronque — et livre à node un fragment amputé qui
    # s'exécute très bien. Le banc restait vert sur le défaut qu'il devait attraper (vérifié par
    # mutation). C'est `tools/verif_rendu_settings.py` qui couvre cette page, en exécutant le
    # BLOC <script> entier.
}

# Matrice d'arguments par fonction. Chaque entrée est une expression JS évaluée dans le harnais ;
# le produit cartésien est exploité. On vise les CAS DE BORD (valeurs nulles, champs absents,
# états mutuellement exclusifs), pas la couverture fonctionnelle.
COTES = "['in', 'out']"
PORTS = """[
  {kind:'video', shm:'x_0'}, {kind:'audio', shm:'x_audio_0'}, {kind:'data', shm:'x_anc_0'},
  {kind:'video', shm:''}, {kind:'video'}, {},
  {kind:'video', shm:'a', fed:true}, {kind:'video', shm:'a', fed:false},
  {kind:'audio', shm:'a', fed:false}, {kind:'data', shm:'a', fed:true},
  {kind:'video', shm:'a', free:true}, {kind:'video', shm:'a', disconnected:true},
  {kind:'video', shm:'a', rx_stalled:true}, {kind:'video', shm:'a', rx_latency_ms:12},
  {kind:'video', shm:'a', fps:50}, {kind:'video', shm:'a', fps:0},
  {kind:'video', shm:'a', format:'1080p50'}, {kind:'video', shm:'a', slot:3},
  {kind:'video', shm:'a', delay_total_ms:40}, {kind:'video', shm:'a', lag_frames:2},
  {kind:'video', shm:'a', label:'Entrée 1'}, {kind:'video', shm:'a', stale:true, age_ms:9000}
]"""
NOEUDS = """[
  {vmid:1, hostname:'h', kind:'mixer', produces:[], consumes:[], col:'composition'},
  {vmid:2, hostname:'h2', kind:'2110_io', status:'running', fps:50,
   produces:[{kind:'video', shm:'a', fed:true}, {kind:'audio', shm:'b', fed:false}],
   consumes:[{kind:'video', shm:'c'}, {kind:'video', shm:'', disconnected:true}],
   col:'sources', split:true, node_label:'n1', max_inputs:4, slots_free:2},
  {vmid:3, hostname:'h3', produces:[], consumes:[], project_module:true, project_state:'saved'},
  {vmid:4, hostname:'h4', produces:[{kind:'video'}], consumes:[], fabric_role:'logical',
   fabric_shards:3, cpu_percent:80, mem_used:1024, gpu:{}, av_sync:{}, own_latency_ms:5},
  // Verdict de CONTENU NEUF (`contenu_etat`, posé par home_dashboard._contenu_etats) : les trois
  // issues doivent se rendre — décrochage simple, décrochage imputé à un shard nommé, et le cas
  // « pas de référence » qui ne doit RIEN afficher plutôt qu'inventer une alarme.
  {vmid:5, hostname:'h5', produces:[], consumes:[], fps:50,
   contenu_etat:{ref:50, mesure:38, tenue:false, maillon:null, maillon_mesure:null}},
  {vmid:6, hostname:'h6', produces:[], consumes:[], fps:50,
   contenu_etat:{ref:25, mesure:50, tenue:false, maillon:'shard-a', maillon_mesure:0}},
  {vmid:7, hostname:'h7', produces:[], consumes:[], fps:50,
   contenu_etat:{ref:null, mesure:null, tenue:null, maillon:null, maillon_mesure:null}},
  {vmid:8, hostname:'h8', produces:[], consumes:[], fps:50, contenu_etat:{}}
]"""
ARGS = {
    "fmtSummary": ["[null, undefined, '', '1080p50', {width:1920,height:1080,fps:50}, {}]"],
    "renderPort": [COTES, PORTS, "[0, 3]", "['plugin', 'project', null]", "[42, null]"],
    "renderPlaceholderInput": ["[0, 5]", "['plugin', 'project', null]"],
    "renderNode": [NOEUDS],
    # Ces deux-là lisent des globales de page (bouchonnées au point fixe) et ne prennent pas
    # d'argument : la matrice à un cas vide suffit, on veut seulement qu'elles S'EXÉCUTENT.
    "_tslRenderConns": ["[undefined]"],
    "_tallyEntetes": ["[undefined]"],
}

# Bouchons qu'on ne peut pas deviner : le point fixe crée par défaut une fonction rendant '',
# ce qui convient à un helper de rendu mais pas à une valeur consultée comme donnée.
BOUCHONS_FIXES = {
    "escapeHtml": "s => String(s == null ? '' : s)",
    "T": "k => String(k)",
    "window": "{t: k => String(k), location: {hash: ''}}",
    "document": "{getElementById: () => null, querySelector: () => null, "
                "querySelectorAll: () => [], createElement: () => ({style:{}, classList:{add(){},remove(){}}})}",
    "LABEL_LEVEL": "0",
    "_latMode": "'sum'",
    # Page Réglages : ce que les fonctions de rendu consultent comme DONNÉE et non comme helper.
    "_tslConns": "[{id:1, name:'c', port:9001, enabled:1, label_col:2, level_uuid:'u-1',"
                 " rouge_field:'tt', vert_field:'lh', direction:'in', dest_host:'', status:{}}]",
    "_tslNiveaux": "[{uuid:'u-1', nom:'Antenne', num:1, owner_kind:'connection', owner_id:1}]",
    "_tslLabelNames": "['Hostname','MXL','L2','L3','L4','L5','L6','L7','L8','L9']",
    "_tslProjects": "[]",
    "_tslMapByConn": "{}",
    "setT": "k => String(k)",
    "apEsc": "s => String(s == null ? '' : s)",
}

MAX_ITER = 80          # garde-fou du point fixe : au-delà, c'est que node renvoie autre chose


def extraire_fonction(html, nom):
    """Corps complet d'une fonction top-level du HTML rendu, ou None.

    Découpage par COMPTAGE D'ACCOLADES et non par expression régulière gourmande : une fonction de
    rendu contient des gabarits, des objets et des fonctions imbriquées, qu'un `.*?\\n\\}` couperait
    au premier `}` en début de ligne — on livrerait alors à node un fragment tronqué, et l'erreur
    de syntaxe qui en résulte n'aurait aucun rapport avec le code réel.
    """
    m = re.search(r"^function %s\s*\(" % re.escape(nom), html, re.M)
    if not m:
        return None
    j = html.index("{", m.start())
    prof = 0
    # Le scanner DOIT connaître les commentaires : le code est commenté en français, et une
    # apostrophe de « n'existe » basculerait un scanner naïf en mode chaîne pour tout le reste du
    # fichier — il rendrait alors un fragment tronqué, et l'erreur de syntaxe qui en découle
    # n'aurait aucun rapport avec le code réel. Payé une fois, sur renderPort.
    # Les gabarits `...${ ... }...` ont aussi besoin d'une pile : le `}` qui ferme une interpolation
    # ne ferme pas un bloc, et les guillemets qui vivent DEDANS sont du code, pas du texte.
    pile = []          # 'tpl' = dans un gabarit, 'itp' = dans un ${} de gabarit
    chaine = ""        # délimiteur de chaîne simple en cours ("'" ou '"'), sinon ""
    while j < len(html):
        ch, suiv = html[j], html[j + 1:j + 2]
        if chaine:
            if ch == "\\":
                j += 2
                continue
            if ch == chaine:
                chaine = ""
        elif pile and pile[-1] == "tpl":
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                pile.pop()
            elif ch == "$" and suiv == "{":
                pile.append("itp")
                j += 2
                continue
        elif ch == "/" and suiv == "/":
            j = html.find("\n", j)
            if j < 0:
                return None
        elif ch == "/" and suiv == "*":
            k = html.find("*/", j + 2)
            if k < 0:
                return None
            j = k + 1
        elif ch in "\"'":
            chaine = ch
        elif ch == "`":
            pile.append("tpl")
        elif ch == "{":
            prof += 1
        elif ch == "}":
            if pile and pile[-1] == "itp":
                pile.pop()                 # fin d'interpolation, pas fin de bloc
            else:
                prof -= 1
                if prof == 0:
                    return html[m.start():j + 1]
        j += 1
    return None


def rendre_page(page):
    """HTML réellement servi pour `page`, via le client de test Flask (session forgée)."""
    import main
    from app.database import get_db
    app = main.app
    app.config["TESTING"] = True
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    if not u:
        raise SystemExit("aucun utilisateur en base : impossible de forger une session")
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]
    r = cli.get(page)
    if r.status_code != 200:
        raise SystemExit("%s a répondu %s" % (page, r.status_code))
    return r.data.decode("utf-8", "replace")


def harnais(source, nom, listes, bouchons):
    """Programme node : bouchons, fonction sous test, produit cartésien des arguments."""
    decl = "\n".join("var %s = %s;" % (k, v) for k, v in bouchons.items())
    tabs = "[%s]" % ", ".join(listes)
    return """
%s
%s
const LISTES = %s;
function produit(ls) {
  return ls.length === 0 ? [[]] : ls[0].flatMap(v => produit(ls.slice(1)).map(r => [v].concat(r)));
}
const cas = produit(LISTES);
let ok = 0; const ko = [];
for (const args of cas) {
  try {
    const out = %s.apply(null, args);
    if (out !== undefined && out !== null && typeof out !== 'string' && typeof out !== 'number')
      throw new Error('type de retour inattendu : ' + typeof out);
    ok++;
  } catch (e) {
    if (/is not defined/.test(e.message)) { console.log('MANQUE:' + e.message); process.exit(7); }
    ko.push(JSON.stringify(args).slice(0, 160) + ' -> ' + e.message);
  }
}
console.log(JSON.stringify({total: cas.length, ok: ok, ko: ko.slice(0, 8), n_ko: ko.length}));
""" % (decl, source, tabs, nom)


def executer(source, nom, listes):
    """Exécute jusqu'au point fixe des bouchons. Retourne (rapport, globales_bouchonnées)."""
    bouchons = dict(BOUCHONS_FIXES)
    decouvertes = []
    for _ in range(MAX_ITER):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(harnais(source, nom, listes, bouchons))
            chemin = fh.name
        try:
            p = subprocess.run(["node", chemin], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(chemin)
        sortie = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"(?:MANQUE:)?(\w+) is not defined", sortie)
        if m and p.returncode != 0:
            sym = m.group(1)
            if sym in bouchons:          # bouchonné et toujours manquant : on ne boucle pas
                return {"erreur": "bouchon inopérant pour %s" % sym}, decouvertes
            bouchons[sym] = "function(){ return ''; }"
            decouvertes.append(sym)
            continue
        ligne = [l for l in (p.stdout or "").splitlines() if l.startswith("{")]
        if not ligne:
            return {"erreur": (sortie.strip().splitlines() or ["(aucune sortie)"])[0]}, decouvertes
        return json.loads(ligne[-1]), decouvertes
    return {"erreur": "point fixe des bouchons non atteint en %d itérations" % MAX_ITER}, decouvertes


def main_():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page", action="append", help="page à vérifier (défaut : toutes)")
    ap.add_argument("--fn", action="append", help="fonction à vérifier (défaut : toutes)")
    ap.add_argument("--deps", action="store_true", help="afficher les globales bouchonnées")
    a = ap.parse_args()

    if subprocess.run(["which", "node"], capture_output=True).returncode != 0:
        raise SystemExit("node introuvable — requis pour exécuter les fonctions de rendu")

    pages = a.page or list(CIBLES)
    echecs = 0
    for page in pages:
        html = rendre_page(page)
        print("=== %s ===" % page)
        for nom in (a.fn or CIBLES.get(page, [])):
            src = extraire_fonction(html, nom)
            if src is None:
                print("  %-24s INTROUVABLE dans la page rendue (renommée ? supprimée ?)" % nom)
                echecs += 1
                continue
            listes = ARGS.get(nom)
            if not listes:
                print("  %-24s pas de matrice d'arguments déclarée — ignorée" % nom)
                continue
            rap, deps = executer(src, nom, listes)
            if "erreur" in rap:
                print("  %-24s ÉCHEC : %s" % (nom, rap["erreur"]))
                echecs += 1
                continue
            marque = "OK " if rap["n_ko"] == 0 else "KO "
            print("  %s%-22s %d cas, %d ok, %d échec(s)"
                  % (marque, nom, rap["total"], rap["ok"], rap["n_ko"]))
            for l in rap["ko"]:
                print("       %s" % l)
            if a.deps and deps:
                print("       globales bouchonnées : %s" % ", ".join(sorted(deps)))
            echecs += rap["n_ko"]

    if echecs:
        print("\nÉchec : %d cas en erreur — une fonction de rendu casse à l'exécution." % echecs,
              file=sys.stderr)
        return 1
    print("\nOK : toutes les fonctions de rendu s'exécutent sur toute la matrice.")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
