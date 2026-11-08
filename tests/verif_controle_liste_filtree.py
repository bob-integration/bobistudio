#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du contrôle de CATALOGUE « liste filtrée » (`MXLControls.listFilter`, 2026-11-07).
#
# POURQUOI IL EXISTE. Seize pages avaient chacune leur champ de recherche et leur JS ; seul le
# style était partagé. Le contrôle fait le travail une fois, la page Projets en est le premier
# usage. Ce banc tient ce qui ne se voit pas en lisant la barre :
#   · la recherche ignore casse et ACCENTS (« regie » trouve « Régie ») et exige TOUS les mots ;
#   · un filtre en puces se cumule avec la recherche, la première puce vaut « tout » ;
#   · le tri RÉORDONNE les éléments dans leur parent (descendant compris), sans les recréer ;
#   · le compteur dit « n / total » quand on filtre, et le message « rien » n'apparaît qu'alors ;
#   · le tri et les puces sont retenus (`storeKey`), la recherche jamais.
#
#   $ ./venv/bin/python tests/verif_controle_liste_filtree.py
#
# ⚠ Garde MUTÉE à l'écriture.
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
echecs = []


def controle(intitule, condition, detail=""):
    if not condition:
        echecs.append(intitule)
    print("  %-5s %s%s" % ("OK" if condition else "ÉCHEC", intitule, "" if condition else "  [%s]" % (detail,)))


# DOM minimal : de VRAIS éléments, déplacés par appendChild comme dans un navigateur (un élément
# ajouté ailleurs QUITTE son ancien parent — c'est ce qui fait le tri).
DOM = r"""
class El {
  constructor(t){ this.tag=t; this.children=[]; this.className=''; this._txt=''; this.parentNode=null;
    this.dataset={}; this.hidden=false; this.attrs={}; this.value=''; this.type=''; this.id='';
    const self=this;
    this.classList={ add:c=>{ if(!self.className.split(' ').includes(c)) self.className=(self.className+' '+c).trim(); },
      remove:c=>{ self.className=self.className.split(' ').filter(x=>x!==c).join(' '); },
      toggle:(c,on)=>{ on?self.classList.add(c):self.classList.remove(c); },
      contains:c=>self.className.split(' ').includes(c) }; }
  set textContent(v){ this._txt=String(v); if (v==='') this.children=[]; }
  get textContent(){ return this._txt + this.children.map(c=>c.textContent).join(''); }
  appendChild(c){ if (c.parentNode) c.parentNode.children=c.parentNode.children.filter(x=>x!==c);
    c.parentNode=this; this.children.push(c); return c; }
  setAttribute(k,v){ this.attrs[k]=String(v); }
  getAttribute(k){ return this.attrs[k]; }
  all(){ let r=[]; for (const c of this.children) r=r.concat([c], c.all()); return r; }
  querySelectorAll(sel){ const cls=sel.replace(/^\./,''); return this.all().filter(e=>e.classList.contains(cls)); }
}
const STORE={};
global.window={};
global.localStorage={ getItem:k=>(k in STORE?STORE[k]:null), setItem:(k,v)=>{STORE[k]=String(v);} };
global.document={ createElement:t=>new El(t) };
function item(parent, nom, etat, n){ const e=new El('li'); e.dataset.search=nom; e.dataset.etat=etat;
  e.dataset.n=String(n); e.textContent=nom; parent.appendChild(e); return e; }
function visibles(list){ return list.children.filter(e=>!e.hidden).map(e=>e.dataset.search); }
function puce(hote, v){ return hote.all().find(e=>e.tag==='button' && e.dataset.value===v); }
function champ(hote){ return hote.all().find(e=>e.tag==='input'); }
function menu(hote){ return hote.all().find(e=>e.tag==='select'); }
function compteur(hote){ return hote.all().find(e=>e.classList.contains('filter-count-num')).textContent; }
function vide(hote){ return hote.all().find(e=>e.classList.contains('ctl-filter-empty')); }
function monter(hote, list){ return window.MXLControls.listFilter(hote, {
  items: ()=>list.children, search:'Chercher', countLabel:'éléments', empty:'Rien', storeKey:'k',
  groups:[{key:'etat', options:[{value:'all',label:'Tous'},{value:'on',label:'En marche'},{value:'off',label:'Arrêtés'}],
           match:(el,v)=>el.dataset.etat===v}],
  sorts:[{value:'nom',label:'Nom',by:el=>el.dataset.search},
         {value:'taille',label:'Taille',by:el=>+el.dataset.n, desc:true}] }); }
"""


def executer(corps):
    ctl = open(os.path.join(RACINE, "static", "js", "controls.js"), encoding="utf-8").read()
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(DOM + ctl + "\nconst out={};\ntry {\n" + corps
            + "\n} catch (e) { out.exception = String(e && e.stack || e); }\nconsole.log(JSON.stringify(out));\n")
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        ligne = (r.stdout or "").strip().split("\n")[-1]
        return json.loads(ligne) if ligne.startswith("{") else {"exception": (r.stderr or "")[:400]}
    finally:
        os.unlink(f.name)


print("Catalogue — le contrôle « liste filtrée »\n")

res = executer(r"""
const hote=new El('div'), list=new El('ul');
item(list,'Projet Régie Légère mixer recorder','off',18);
item(list,'Banc mixer multiview','on',4);
item(list,'Essai delay udc','off',5);
item(list,'atelier stills','on',2);
monter(hote, list);
out.depart = visibles(list);
out.compteur0 = compteur(hote);
const q=champ(hote);
q.value='regie'; q.oninput(); out.accent = visibles(list);
q.value='MIXER banc'; q.oninput(); out.mots = visibles(list);
out.compteur1 = compteur(hote);
q.value='introuvable'; q.oninput(); out.vide_vu = !vide(hote).hidden; out.compteur2 = compteur(hote);
q.value='mixer'; q.oninput(); out.vide_cache = vide(hote).hidden;
puce(hote,'off').onclick(); out.cumul = visibles(list);
out.pressed = puce(hote,'off').getAttribute('aria-pressed') + '/' + puce(hote,'all').getAttribute('aria-pressed');
puce(hote,'all').onclick(); q.value=''; q.oninput();
const sel=menu(hote); sel.value='taille'; sel.onchange();
out.taille = list.children.map(e=>e.dataset.n);
out.memes = list.children.every(e=>e.parentNode===list) && list.children.length===4;
puce(hote,'on').onclick();
out.memo = JSON.parse(localStorage.getItem('k'));
// Remonter : tri et puce retrouvés, recherche non.
const hote2=new El('div'); monter(hote2, list);
out.remonte = {sort: menu(hote2).value, puce: puce(hote2,'on').classList.contains('active'),
               q: champ(hote2).value, vis: visibles(list)};
""")

if "exception" in res:
    controle("le contrôle s'exécute", False, res["exception"])
else:
    controle("au départ tout est visible, trié par le 1er tri (nom)",
             res["depart"] == ["atelier stills", "Banc mixer multiview", "Essai delay udc",
                               "Projet Régie Légère mixer recorder"] and res["compteur0"] == "4", res)
    controle("« regie » trouve « Régie » (accents et casse ignorés)",
             res["accent"] == ["Projet Régie Légère mixer recorder"], res["accent"])
    controle("plusieurs mots : tous requis", res["mots"] == ["Banc mixer multiview"], res["mots"])
    controle("compteur « n / total » quand on filtre", res["compteur1"] == "1 / 4", res["compteur1"])
    controle("« rien ne correspond » montré seulement quand rien ne passe",
             res["vide_vu"] and res["vide_cache"] and res["compteur2"] == "0 / 4", res)
    controle("la puce se cumule avec la recherche", res["cumul"] == ["Projet Régie Légère mixer recorder"], res["cumul"])
    controle("aria-pressed suit la puce active", res["pressed"] == "true/false", res["pressed"])
    controle("tri descendant par taille : éléments réordonnés dans leur parent",
             res["taille"] == ["18", "5", "4", "2"] and res["memes"], res["taille"])
    controle("tri et puce retenus, recherche non",
             res["memo"] == {"sort": "taille", "groups": {"etat": "on"}}
             and res["remonte"]["sort"] == "taille" and res["remonte"]["puce"]
             and res["remonte"]["q"] == ""
             and res["remonte"]["vis"] == ["Banc mixer multiview", "atelier stills"], res)

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    sys.exit(1)
print("OK")
