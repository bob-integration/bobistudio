#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du contrôle de CATALOGUE « choix multiple » (`MXLControls.chooseList`).
#
# POURQUOI IL EXISTE. Un `<select multiple>` tient tant qu'il y a quatre entrées ; à vingt il
# devient inutilisable — boîte de quatre lignes à faire défiler, Ctrl+clic pour désélectionner qui
# ne s'invente pas, et surtout la sélection est DANS la liste au lieu d'être à côté : on ne voit
# pas ce qu'on a choisi sans tout parcourir. Ici la liste sert à AJOUTER, les puces disent l'état.
#
# CE QU'IL PROTÈGE, et qui ne se voit pas en lisant le code :
#   · reproposer ce qui est déjà choisi — l'exploitant ajoute un doublon que le contrôle refuse
#     en silence, et le menu s'allonge de ce qu'il vient de régler ;
#   · perdre une valeur qui a disparu de la liste (un niveau supprimé ailleurs) : la masquer la
#     ferait disparaître au premier enregistrement, sans que personne l'ait demandé ;
#   · rendre autre chose qu'une LISTE, ce qui reproduirait le défaut d'origine.
#
#   $ ./venv/bin/python tools/verif_controle_choix.py
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


# Un DOM minimal : le contrôle fabrique de VRAIS éléments, on les compte. Bouchonner le rendu
# ferait passer un contrôle qui n'affiche rien.
DOM = """
class El {
  constructor(t){ this.tag=t; this.children=[]; this.className=''; this._txt='';
    this.value=''; this.disabled=false; this.type=''; this.onclick=null; this.onchange=null; }
  // ⚠ `textContent = ''` VIDE LES ENFANTS dans un vrai DOM, et c'est ainsi que le contrôle
  // se redessine. Un faux qui se contente de ranger la chaîne fait s'accumuler les puces à
  // chaque redessin — le banc voyait alors 3 puces là où il y en a 2, et accusait le contrôle.
  set textContent(v){ this._txt = v; if (v === '') this.children = []; }
  get textContent(){ return this._txt; }
  appendChild(c){ this.children.push(c); return c; }
  setAttribute(){}
  get txt(){ return this._txt + this.children.map(c=>c.txt).join(''); }
  tous(cls){ let r = (this.className||'').split(' ').includes(cls) ? [this] : [];
    for (const c of this.children) r = r.concat(c.tous ? c.tous(cls) : []); return r; }
}
global.window = {};
global.document = { createElement: (t) => new El(t), activeElement: null,
                    createTextNode: (t) => { const e = new El('#text'); e.textContent = t; return e; } };
"""


def executer(corps):
    ctl = open(os.path.join(RACINE, "static", "js", "controls.js"), encoding="utf-8").read()
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(DOM + ctl + corps)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        ligne = (r.stdout or "").strip().split("\n")[-1]
        return json.loads(ligne) if ligne.startswith("{") else {"exception": (r.stderr or "")[:250]}
    finally:
        os.unlink(f.name)


print("Catalogue — le contrôle « choix multiple »\n")

res = executer("""
const hote = new El('div');
const OPTS = Array.from({length:20}, (_,i)=>({value:'u-'+i, label:(i+1)+' — Niveau '+(i+1)}));
let dernier = null;
const out = {};
try {
  const api = window.MXLControls.chooseList(hote, {
    options: OPTS, valeurs: ['u-3'], onChange: (v) => { dernier = v; },
    vide: 'AUCUN', ajouter: 'AJOUTER' });
  const sel = () => hote.tous('ctl-choix-select')[0];
  const puces = () => hote.tous('ctl-choix-puce');
  out.depart = puces().length;
  out.valeurDepart = api.value();
  out.proposees = sel().children.length - 1;
  sel().value = 'u-7'; sel().onchange();
  out.apresAjout = puces().length; out.rappelAjout = dernier;
  // ⚠ ON RETIRE LA SECONDE, pas la première. Cliquer la première ne distingue pas « retirer
  // celle qu'on vise » de « retirer la première venue » : une mutation l'a montré, et c'est
  // exactement le défaut qui fait qu'on supprime la mauvaise entrée.
  sel().value = 'u-9'; sel().onchange();
  out.avantRetrait = api.value();
  puces()[1].tous('ctl-choix-x')[0].onclick();
  out.apresRetrait = puces().length; out.rappelRetrait = dernier;
  api.set(OPTS.map(o=>o.value));
  out.toutChoisi = puces().length; out.menuDesactive = sel().disabled;
  api.options([{value:'u-0', label:'1 — Niveau 1'}]);
  const orph = hote.tous('ctl-choix-inconnue');
  out.orphelines = orph.length;
  // Le TEXTE de la puce doit encore identifier la valeur : une puce anonyme dit qu'il y a un
  // problème sans dire lequel, et on ne peut même pas savoir laquelle retirer.
  out.orphTexte = orph.length ? orph[orph.length - 1].txt : '';
  api.set([]);
  out.vide = hote.tous('ctl-choix-vide').map(e => e.txt);
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
""")

controle("★★ le contrôle s'exécute et fabrique de vrais éléments", not res.get("exception"),
         "obtenu %r" % res.get("exception"))
controle("★★ il affiche une puce par valeur de départ", res.get("depart") == 1)
controle("★★★ il rend une LISTE, jamais une valeur seule",
         res.get("valeurDepart") == ["u-3"],
         "rendre un scalaire reproduirait le défaut d'origine. Obtenu %r" % res.get("valeurDepart"))
controle("★★★ ce qui est DÉJÀ CHOISI ne se propose plus",
         res.get("proposees") == 19,
         "le reproposer invite à un doublon que le contrôle refuse en silence, et allonge le "
         "menu de ce qu'on vient de régler. Attendu 19 sur 20, obtenu %r" % res.get("proposees"))
controle("★★★ ajouter pose une puce ET prévient l'appelant",
         res.get("apresAjout") == 2 and res.get("rappelAjout") == ["u-3", "u-7"],
         "obtenu %r / %r" % (res.get("apresAjout"), res.get("rappelAjout")))
controle("★★★ retirer enlève LA puce visée — pas la première venue",
         res.get("avantRetrait") == ["u-3", "u-7", "u-9"]
         and res.get("apresRetrait") == 2 and res.get("rappelRetrait") == ["u-3", "u-9"],
         "on clique la SECONDE : une croix qui retire la première entrée passerait le test si "
         "on cliquait celle-ci. Avant %r, après %r"
         % (res.get("avantRetrait"), res.get("rappelRetrait")))
controle("★★ quand tout est choisi, le menu se désactive au lieu de rester vide",
         res.get("toutChoisi") == 20 and res.get("menuDesactive") is True,
         "un menu ouvert sur rien laisse croire à une panne. Obtenu %r" % res.get("menuDesactive"))
controle("★★★ une valeur DISPARUE de la liste reste affichée, signalée, et IDENTIFIABLE",
         res.get("orphelines") == 19 and "u-19" in (res.get("orphTexte") or ""),
         "la masquer la ferait disparaître au premier enregistrement sans que personne l'ait "
         "demandé — or c'est le cas qui mérite un regard : le niveau a été supprimé ailleurs. "
         "Obtenu %r" % res.get("orphelines"))
controle("★★ rien de choisi → on le DIT, on ne laisse pas un vide",
         res.get("vide") == ["AUCUN"],
         "une zone vide ne se distingue pas d'un contrôle en panne. Obtenu %r" % res.get("vide"))

# ── Le style existe, sinon le contrôle est nu là où on le dépose ─────────────
css = open(os.path.join(RACINE, "static", "css", "controls.css"), encoding="utf-8").read()
manquantes = [c for c in ("ctl-choix", "ctl-choix-puce", "ctl-choix-x", "ctl-choix-select",
                          "ctl-choix-vide", "ctl-choix-inconnue") if ".%s" % c not in css]
controle("★★ toutes ses classes sont stylées", not manquantes,
         "un contrôle de catalogue doit être autonome partout où on le dépose — y compris sur "
         "une page de plugin, qui n'a pas le style scopé `.form`. Manquantes : %s" % manquantes)

# ── UN CLIC QU'ON N'A PAS REÇU ───────────────────────────────────────────────
# Déposé dans un <label> — ce qui arrive — le contrôle voit TOUT clic de l'étiquette renvoyé vers
# son premier bouton : cliquer À CÔTÉ d'une puce en supprimait une autre. Signalé le 2026-09-01 :
# « si j'appuie à droite de la case pour supprimer le premier niveau, ça le supprime quand même ».
# ⚠ Et il ne suffit pas de refuser `detail === 0` : une activation au CLAVIER vaut aussi 0. Ce
# qui sépare les deux, c'est le FOCUS.
res = executer("""
const hote = new El('div');
const OPTS = [{value:'a',label:'A'},{value:'b',label:'B'},{value:'c',label:'C'}];
const out = {};
try {
  const api = window.MXLControls.chooseList(hote, {options: OPTS, valeurs: ['a','b','c']});
  const croix = () => hote.tous('ctl-choix-puce')[0].tous('ctl-choix-x')[0];
  // 1. Clic renvoyé par un <label> : synthétique, et le bouton n'a PAS le focus.
  document.activeElement = null;
  croix().onclick({detail: 0, isTrusted: true, preventDefault(){}, stopPropagation(){}});
  out.apresLabel = api.value();
  // 2. Activation au CLAVIER : synthétique aussi, mais le bouton A le focus.
  const c2 = croix();
  document.activeElement = c2;
  c2.onclick({detail: 0, isTrusted: true, preventDefault(){}, stopPropagation(){}});
  out.apresClavier = api.value();
  // 3. Vrai clic de souris.
  document.activeElement = null;
  croix().onclick({detail: 1, isTrusted: true, preventDefault(){}, stopPropagation(){}});
  out.apresSouris = api.value();
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
""")
controle("★★★ un clic renvoyé par un <label> est IGNORÉ",
         res.get("apresLabel") == ["a", "b", "c"],
         "cliquer à côté d'une puce supprimait la PREMIÈRE : le <label> d'accueil renvoie tout "
         "clic vers le premier bouton qu'il contient. Obtenu %r" % (res.get("apresLabel"),))
controle("★★★ mais l'activation au CLAVIER fonctionne",
         res.get("apresClavier") == ["b", "c"],
         "Entrée et Espace produisent aussi un clic synthétique : les refuser rendrait le "
         "contrôle inutilisable sans souris. Obtenu %r" % (res.get("apresClavier"),))
controle("★★ et le clic de souris aussi", res.get("apresSouris") == ["c"],
         "obtenu %r" % (res.get("apresSouris"),))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
