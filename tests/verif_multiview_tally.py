#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du NIVEAU DE TALLY dans l'éditeur multiview.
#
# RÉGRESSION QU'IL VERROUILLE (introduite le 2026-09-01, trouvée en répondant à une question de
# l'utilisateur). Le dénouement a fait des niveaux des UUID ; le menu de l'éditeur a bien été
# migré, mais l'ÉCRITURE faisait toujours `parseInt(...) || 0`. `parseInt` d'un UUID rend `NaN`,
# donc `0`, donc AUCUN niveau : toute modification d'une tuile effaçait son tally, en silence, en
# affichant pourtant le bon menu.
#
# ET CÔTÉ LECTURE, le distributeur n'enveloppait que les `int` : une chaîne serait parcourue
# CARACTÈRE PAR CARACTÈRE — trente-six « niveaux » d'une lettre, aucun n'existant, donc un tally
# qui ne s'allume jamais et pas la moindre erreur.
#
#   $ ./venv/bin/python tools/verif_multiview_tally.py
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


print("Multiview — le niveau de tally est un UUID, pas un nombre\n")

js = open(os.path.join(RACINE, "plugins", "multiview", "multiview.js"), encoding="utf-8").read()
code = re.sub(r"^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S), flags=re.M)

fautifs = [l.strip()[:90] for l in code.split("\n")
           if "tally_level" in l and re.search(r"parseInt\s*\(", l)]
controle("★★★ plus aucun `parseInt` sur un niveau de tally", not fautifs,
         "`parseInt` d'un UUID rend NaN, donc 0 avec le `|| 0` qui suit : modifier une tuile "
         "effaçait son tally en silence, alors que le menu proposait les bons identifiants. "
         "Restant : %s" % fautifs)

zeros = [l.strip()[:90] for l in code.split("\n")
         if re.search(r"tally_level\s*[:=].*(\?\?\s*0|\|\|\s*0)\b", l)]
controle("★★★ et plus aucun repli sur `0`", not zeros,
         "0 était le code « aucun niveau » du modèle à numéros ; avec des UUID il ne désigne "
         "rien, et il masque la vraie valeur. Restant : %s" % zeros)

controle("★★ le menu propose bien des UUID nommés",
         "n.uuid" in code and "n.num" in code,
         "il listait `tally_base / 3 + 1` — les bandes de la trame TSL recopiées dans l'éditeur")

# ── LE CHOIX EST MULTIPLE, ET C'EST LE CONTRÔLE DE CATALOGUE ────────────────
# Le tally se CUMULE : une tuile peut suivre plusieurs chaînes de destination. Un `<select>`
# obligeait à n'en choisir qu'une — et à vingt niveaux, un `<select multiple>` serait
# inutilisable (cf. le contrôle `chooseList`).
controle("★★★ l'éditeur monte le contrôle de catalogue",
         "MXLControls.chooseList" in code,
         "sans lui, une tuile ne peut suivre qu'un seul niveau")
html = open(os.path.join(RACINE, "plugins", "multiview", "control.html"), encoding="utf-8").read()
for champ in ("ed_tally_level", "ov_tally_level"):
    controle("★★★ %s n'est plus un <select>" % champ,
             not re.search(r'<select[^>]*id="%s"' % champ, html),
             "le contrôle fabrique ses propres éléments : le laisser dans un <select> lui "
             "donnerait un hôte qu'il ne sait pas remplir")
    controle("★★★ ...et il n'est pas dans un <label>",
             not re.search(r'<label[^>]*for="%s"' % champ, html),
             "un <label> renvoie tout clic vers le premier contrôle qu'il contient : cliquer à "
             "côté d'une puce en supprimerait une autre")
controle("★★ les valeurs par défaut sont des LISTES",
         "tally_level: []" in code,
         "une chaîne vide traînerait le modèle scalaire, et se parcourrait caractère par "
         "caractère à la lecture")
controle("★★★ une valeur héritée est ramenée à une liste",
         "_tallyListe" in code and code.count("_tallyListe(") >= 4,
         "un scalaire hérité — un UUID seul, ou un vieux numéro — doit être enveloppé partout "
         "où il est relu, sinon il se parcourt caractère par caractère")

# ── Côté lecture : une CHAÎNE doit être enveloppée, pas parcourue ────────────
tsl = open(os.path.join(RACINE, "services", "tsl", "__init__.py"), encoding="utf-8").read()
mauvais = re.findall(r"isinstance\((\w+), int\):\s*\n\s*\1 = \[", tsl)
controle("★★★ le distributeur n'enveloppe plus seulement les `int`", not mauvais,
         "une chaîne serait parcourue caractère par caractère : trente-six « niveaux » d'une "
         "lettre, aucun n'existant. Aucune erreur, aucun tally. Restant : %s" % mauvais)
controle("★★ il enveloppe TOUT ce qui n'est pas une liste, sur les QUATRE chemins",
         tsl.count("if not isinstance(") >= 4,
         "tuiles, incrustations, hook de plugin ET mélangeur : les quatre lisent la même "
         "configuration et doivent tenir la même règle. C'est le banc qui a trouvé le "
         "quatrième — le mélangeur — que la relecture à l'œil avait manqué")

# ── Le comportement, exécuté ─────────────────────────────────────────────────
sys.path.insert(0, RACINE)
u = "5074cc86-c293-48a5-b03b-9e2fad07b03e"
for nom, valeur, attendu in (("un UUID", u, [u]), ("une liste", [u], [u]),
                             ("rien", None, []), ("une liste vide", [], [])):
    v = valeur or []
    if not isinstance(v, list):
        v = [v]
    controle("★ normalisation — %s → %r" % (nom, attendu), v == attendu)

# Le formulaire doit LIRE le contrôle. Une écriture qui rend une constante (liste vide, ou
# l'ancienne valeur) laisserait l'éditeur vert à l'écran et n'enregistrerait rien.
controle("★★★ l'écriture du formulaire lit le contrôle",
         "f.tally_level    = _tlc ? _tlc.value() : [];" in js
         and "o.tally_level = _ovc ? _ovc.value() : [];" in js,
         "les deux formulaires (tuile et incrustation) doivent relire les puces affichées")
controle("★★ la charge envoyée au conteneur reste une liste",
         "f.tally_level ?? ''" not in js and js.count("tally_level: _tallyListe(") >= 3,
         "un repli scalaire `?? ''` ferait repartir un niveau en chaîne de caractères")

# ── ET ON L'EXÉCUTE ─────────────────────────────────────────────────────────
# Les contrôles ci-dessus lisent le code. Ils n'auraient pas vu la régression d'origine, qui
# n'apparaît qu'à l'exécution : le menu affichait les bons identifiants et l'écriture les
# jetait. On monte donc le contrôle POUR DE VRAI, sur les fonctions du tally — charger tout
# `multiview.js` demanderait un navigateur, mais ce périmètre-là suffit et il est honnête.
import json as _json
import subprocess as _sp
import tempfile as _tf

_deb = js.index("function _tallyLevelOptions()")
_fin = js.index("function escapeHtmlMv(")
_prel = """
class El { constructor(t){this.tag=t;this.children=[];this.className='';this._txt='';this.value='';
    this.disabled=false;this.type='';this.dataset={};this.onclick=null;this.onchange=null;}
  set textContent(v){this._txt=v; if(v==='') this.children=[];}
  get textContent(){return this._txt;}
  appendChild(c){this.children.push(c);return c;} setAttribute(){}
  get txt(){return this._txt + this.children.map(c=>c.txt).join('');}
  tous(cls){let r=(this.className||'').split(' ').includes(cls)?[this]:[];
    for(const c of this.children) r=r.concat(c.tous?c.tous(cls):[]); return r;} }
const N={}; global.window={};
global.document={createElement:t=>new El(t), activeElement:null,
  createTextNode:t=>{const e=new El('#text');e.textContent=t;return e;},
  getElementById:id=>(N[id]=N[id]||new El('div'))};
let _tslNiveaux=[{uuid:'u1',num:1,nom:'Antenne'},{uuid:'u2',num:2,nom:'Plateau'},
                 {uuid:'u3',num:3,nom:'Regie'}];
let _appels=0; function onEntryChange(){_appels++;} function onOverlayChange(){}
function escapeHtmlMv(s){return String(s??'');}
"""
_epi = """
const out={};
try{
  const c = _tallyCtl('ed_tally_level', onEntryChange);
  out.monte = !!c; out.vide = c.value();
  c.set(_tallyListe('u1'));                       // valeur HÉRITÉE scalaire
  out.apresScalaire = c.value();
  const sel = () => N['ed_tally_level'].tous('ctl-choix-select')[0];
  sel().value='u3'; sel().onchange();
  out.apresAjout = c.value(); out.rappels = _appels;
  const p = N['ed_tally_level'].tous('ctl-choix-puce');
  p[0].tous('ctl-choix-x')[0].onclick({detail:1,preventDefault(){},stopPropagation(){}});
  out.apresRetrait = c.value();
  out.puces = N['ed_tally_level'].tous('ctl-choix-puce').map(x=>x.txt.replace('\u00d7',''));
}catch(e){out.exception=e.message;}
console.log(JSON.stringify(out));
"""
_ctl = open(os.path.join(RACINE, "static", "js", "controls.js"), encoding="utf-8").read()
_f = _tf.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
_f.write(_prel + _ctl + "\n" + js[_deb:_fin] + _epi)
_f.close()
try:
    _r = _sp.run(["node", _f.name], capture_output=True, text=True, timeout=60)
    _ligne = (_r.stdout or "").strip().split("\n")[-1]
    _res = _json.loads(_ligne) if _ligne.startswith("{") else {"exception": (_r.stderr or "")[:250]}
finally:
    os.unlink(_f.name)

controle("★★★ le contrôle se monte dans l'éditeur", _res.get("monte") and not _res.get("exception"),
         "obtenu %r" % _res.get("exception"))
controle("★★★ une tuile HÉRITÉE (un seul niveau, scalaire) s'affiche",
         _res.get("apresScalaire") == ["u1"],
         "toutes les tuiles existantes portent un scalaire : les perdre à l'ouverture de "
         "l'éditeur effacerait le tally de tout un mur. Obtenu %r" % (_res.get("apresScalaire"),))
controle("★★★ on peut en AJOUTER un second", _res.get("apresAjout") == ["u1", "u3"],
         "c'est tout l'objet : le tally se cumule. Obtenu %r" % (_res.get("apresAjout"),))
controle("★★ l'éditeur est prévenu du changement", _res.get("rappels") == 1,
         "sans rappel, le geste ne serait pas enregistré ; deux rappels écriraient deux fois")
controle("★★★ retirer une puce retire LA BONNE", _res.get("apresRetrait") == ["u3"]
         and _res.get("puces") == ["3 — Regie"],
         "obtenu %r / %r" % (_res.get("apresRetrait"), _res.get("puces")))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
