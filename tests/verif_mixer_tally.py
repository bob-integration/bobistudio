#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Le tally du mélangeur : sur SA page, à chaud, en plusieurs niveaux.

Les trois réglages (`tally_emit`, `tally_force`, `tally_level_base`) ne sont lus que par le
service TSL de l'ORCHESTRATEUR, à partir de `deploy_config` — le conteneur ne les voit jamais.
Ils n'ont donc jamais eu besoin d'un redéploiement, et ils n'ont rien à faire au panneau ⚙,
réservé au système.

Le banc lit le manifeste ET EXÉCUTE le bloc de tally avec un DOM feint : lire la source
n'aurait pas vu la régression jumelle de multiview (le menu affichait les bons identifiants et
l'écriture les jetait).
"""
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

reussites, echecs = [], []


def controle(nom, ok, pourquoi=""):
    (reussites if ok else echecs).append(nom)
    print("  %-5s %s%s" % ("OK" if ok else "ÉCHEC", nom, "" if ok else "\n        → " + pourquoi))


MIX = os.path.join(RACINE, "plugins", "mixer")
man = json.load(open(os.path.join(MIX, "plugin.json"), encoding="utf-8"))
js = open(os.path.join(MIX, "control.js"), encoding="utf-8").read()
css = open(os.path.join(MIX, "control.css"), encoding="utf-8").read()
champs = {f["key"]: f for f in man["config_schema"] if f.get("key")}

print("── Manifeste ───────────────────────────────────────────────────────────")
TALLY = ("tally_emit", "tally_force", "tally_level_base")
for k in TALLY:
    controle("★★ « %s » ne redéploie pas" % k, champs.get(k, {}).get("redeploiement") is False,
             "il n'est lu que par l'orchestrateur : annoncer un redéploiement ferait craindre "
             "une coupure de flux à chaque case cochée")
    controle("★★ « %s » n'est plus au panneau ⚙" % k, champs.get(k, {}).get("scope") == "user",
             "le panneau est réservé au système (décision de l'utilisateur, 2026-09-01) ; "
             "`plugin_section.html` n'y rend que les champs `scope: system`")

lvl = champs.get("tally_level_base", {})
controle("★★★ le niveau est une LISTE, pas un nombre", lvl.get("type") == "multiselect",
         "un niveau est un UUID depuis le dénouement : en `number`, `coerce_config` en ferait 0 "
         "— c'est-à-dire « ceux de la production », en silence. Obtenu %r" % lvl.get("type"))
controle("★★ les niveaux viennent du site, pas d'une liste figée",
         lvl.get("options_from") == "tally_levels" and not lvl.get("options"),
         "une liste figée dans le manifeste redeviendrait des bandes de protocole")
controle("★ défaut = liste vide", lvl.get("default") == [],
         "obtenu %r" % (lvl.get("default"),))

print("\n── Le manifeste ne ment pas sur qui lit quoi ───────────────────────────")
tsl = open(os.path.join(RACINE, "services", "tsl", "__init__.py"), encoding="utf-8").read()
scr = open(os.path.join(MIX, "script.py"), encoding="utf-8").read()
controle("★★★ le service TSL lit bien ces trois clés",
         all(('"%s"' % k) in tsl or ("'%s'" % k) in tsl for k in TALLY),
         "si l'orchestrateur cessait de les lire, `redeploiement: false` deviendrait un mensonge "
         "et le réglage n'aurait plus AUCUN effet")
controle("★★★ le CONTENEUR ne les lit pas", not any(k in scr for k in TALLY),
         "s'il les lisait, un changement à chaud n'atteindrait jamais l'image et le réglage "
         "afficherait une valeur que le mélangeur n'applique pas")
controle("★★ le service TSL accepte la liste",
         'niveaux = params.get("tally_level_base") or []' in tsl
         and "if not isinstance(niveaux, list)" in tsl,
         "une chaîne serait parcourue CARACTÈRE PAR CARACTÈRE")

print("\n── La page ─────────────────────────────────────────────────────────────")
controle("★★★ pas de <label> autour de la liste de niveaux",
         'class="mx-tally-titre"' in js and "<label" not in js.split("mx-tally-field")[1][:400],
         "un <label> sans « for » renvoie tout clic vers le premier bouton qu'il contient — "
         "ici la croix de la PREMIÈRE puce : cliquer à côté d'une puce en supprimait une")
controle("★★ contrôle du CATALOGUE, pas un menu maison",
         "MXLControls.chooseList" in js and "<select multiple" not in js,
         "un `<select multiple>` tient à quatre niveaux, pas à vingt")
controle("★★ les switches sont ceux du projet",
         js.count('class="ios-toggle"') == 2 and js.count('class="palette-field-check"') == 2,
         "`plugin_section.html` rend les cases ainsi : une classe inventée ici serait un "
         "contrôle nu")
controle("★★★ la vérité est le config PERSISTÉ, pas /state",
         "/plugin_config`" in js and "tallyCfg" in js,
         "`/state` répond la photo du déploiement : reconstruire dessus ferait réapparaître "
         "ce qu'on vient de retirer")
controle("★★ les gestes sont regroupés avant l'envoi",
         "tallyTimers[cle] = setTimeout" in js and "clearTimeout(tallyTimers[cle])" in js,
         "une liste s'édite par gestes successifs : sans regroupement, trois niveaux choisis "
         "coup sur coup partiraient en trois requêtes concurrentes")
controle("★★ un 409 est réessayé", "r.status === 409" in js,
         "un déploiement venu d'ailleurs occuperait la place et le geste serait perdu en silence")
controle("★ les timers meurent avec la page",
         "Object.keys(tallyTimers).forEach(k=>clearTimeout(tallyTimers[k]));" in js,
         "un envoi différé après démontage écrirait sur un conteneur qu'on ne regarde plus")
controle("★ l'i18n couvre les nouveaux libellés",
         all(k in json.load(open(os.path.join(MIX, "i18n", "fr.json"), encoding="utf-8"))
             and k in json.load(open(os.path.join(MIX, "i18n", "en.json"), encoding="utf-8"))
             for k in ("plugin.mixer.sec_tally", "plugin.mixer.tally_levels",
                       "plugin.mixer.tally_emit", "plugin.mixer.tally_force")),
         "un repli FR en dur laisserait la page à moitié traduite")
controle("★ le CSS dit pourquoi le titre n'est pas un <label>",
         "mx-tally-titre" in css and "<label>" in css,
         "sans la raison écrite, le prochain qui range le CSS remettra un <label>")

print("\n── ET ON L'EXÉCUTE ─────────────────────────────────────────────────────")
_deb = js.index("    async function tallyCharger()")
_fin = js.index("    function tallySet(cle, valeur)")
_fin2 = js.index("\n    // ─── T-Bar")
_prel = """
class El { constructor(t){this.tag=t;this.children=[];this.className='';this._txt='';this.value='';
    this.hidden=false;this.checked=false;this.dataset={};this.onclick=null;this.onchange=null;}
  set textContent(v){this._txt=v; if(v==='') this.children=[];}
  get textContent(){return this._txt;}
  appendChild(c){this.children.push(c);return c;} setAttribute(){}
  get txt(){return this._txt + this.children.map(c=>c.txt).join('');}
  tous(cls){let r=(this.className||'').split(' ').includes(cls)?[this]:[];
    for(const c of this.children) r=r.concat(c.tous?c.tous(cls):[]); return r;} }
const N={}; global.window={};
global.document={createElement:t=>new El(t), activeElement:null,
  createTextNode:t=>{const e=new El('#text');e.textContent=t;return e;}};
const nœud = id => (N[id] = N[id] || new El('div'));
let EL = new El('div'), VMID = 42, tallyCtl = null, TOAST = () => {};
let tallyCfg = null, tallyNiveaux = null;
const tallyEnVol = new Set(), tallyTimers = {};
const T = (k, r) => r;
const $  = sel => nœud(sel.replace('#',''));
const $$ = sel => nœud(sel.replace('#',''));
const POSTS = [];
global.fetch = async (url, opt) => {
  if (opt && opt.method === 'POST'){ POSTS.push(JSON.parse(opt.body)); return {ok:true, status:200}; }
  // ⚠ `tally_force` est ABSENT — le cas d'un mélangeur qui n'y a jamais touché. Son défaut
  // est VRAI : c'est le seul cas où `!!c.tally_force` et `c.tally_force !== false` diffèrent.
  // Le poser à `false` dans ce faux config rendrait la garde INDISTINGUABLE (vérifié : muette).
  if (url.endsWith('/plugin_config')) return {ok:true, json: async()=>({params:{
      tally_emit:true, tally_level_base:'u1'}})};                      // ⚠ scalaire HÉRITÉ
  return {ok:true, json: async()=>([{uuid:'u1',num:1,nom:'Antenne'},
                                    {uuid:'u2',num:2,nom:'Plateau'},{uuid:'u3',num:3,nom:'Regie'}])};
};
"""
_epi = """
(async () => {
  const out = {};
  try {
    await tallyMonter();
    out.emit  = nœud('mx-tally-emit').checked;
    out.force = nœud('mx-tally-force').checked;      // absent ≠ décoché : le défaut est VRAI
    out.herite = tallyCtl.value();
    const sel = () => nœud('mx-tally-levels').tous('ctl-choix-select')[0];
    sel().value = 'u3'; sel().onchange();
    out.apresAjout = tallyCtl.value();
    const p = nœud('mx-tally-levels').tous('ctl-choix-puce');
    p[0].tous('ctl-choix-x')[0].onclick({detail:1, preventDefault(){}, stopPropagation(){}});
    out.apresRetrait = tallyCtl.value();
    out.puces = nœud('mx-tally-levels').tous('ctl-choix-puce').map(x=>x.txt.replace('\\u00d7',''));
    await new Promise(r => setTimeout(r, 700));      // le regroupement doit s'écouler
    out.posts = POSTS;
  } catch(e){ out.exception = e.message; }
  console.log(JSON.stringify(out));
})();
"""
_ctl = open(os.path.join(RACINE, "static", "js", "controls.js"), encoding="utf-8").read()
_f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
_f.write(_prel + _ctl + "\n" + js[_deb:_fin] + js[_fin:_fin2] + _epi)
_f.close()
try:
    _r = subprocess.run(["node", _f.name], capture_output=True, text=True, timeout=60)
    _l = (_r.stdout or "").strip().split("\n")[-1]
    res = json.loads(_l) if _l.startswith("{") else {"exception": (_r.stderr or "")[:300]}
finally:
    os.unlink(_f.name)

controle("★★★ le bloc se monte", not res.get("exception"), "obtenu %r" % res.get("exception"))
controle("★★★ un niveau HÉRITÉ scalaire s'affiche", res.get("herite") == ["u1"],
         "tous les mélangeurs existants portent un scalaire (`tally_level_base` était un "
         "nombre) : le perdre à l'ouverture éteindrait leur tally. Obtenu %r" % (res.get("herite"),))
controle("★★ « émettre » reflète ce qui est enregistré", res.get("emit") is True,
         "obtenu %r" % res.get("emit"))
controle("★★★ « forcer » distingue ABSENT de DÉCOCHÉ", res.get("force") is True,
         "son défaut est VRAI : lire `!!c.tally_force` afficherait décoché pour les deux, et "
         "l'écran contredirait le service TSL")
controle("★★★ on peut en ajouter un second", res.get("apresAjout") == ["u1", "u3"],
         "obtenu %r" % (res.get("apresAjout"),))
controle("★★★ retirer une puce retire LA BONNE",
         res.get("apresRetrait") == ["u3"] and res.get("puces") == ["3 — Regie"],
         "obtenu %r / %r" % (res.get("apresRetrait"), res.get("puces")))
controle("★★★ le geste est ENVOYÉ, et une seule fois",
         res.get("posts") == [{"params": {"tally_level_base": ["u3"]}}],
         "deux gestes coup sur coup ne doivent faire qu'une écriture, portant la valeur "
         "FINALE. Obtenu %r" % (res.get("posts"),))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
