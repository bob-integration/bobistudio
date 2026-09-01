#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de la RAFALE de réglages sur la page d'un plugin — la séquence exacte rapportée le
# 2026-09-01 : « j'ai ajouté les 3 niveaux, supprimé le 1, et le 2 s'est supprimé aussi ».
#
# CE QUI SE PASSAIT. Chaque écriture de réglage REDÉPLOIE le conteneur, et l'orchestrateur refuse
# un second déploiement tant que le premier est en vol (`_plugin_config_pending` → HTTP 409). Une
# sélection multiple s'édite par gestes SUCCESSIFS : trois niveaux choisis coup sur coup faisaient
# trois POST, dont deux repartaient en 409 — PERDUS, alors que les trois puces restaient à
# l'écran. On croyait avoir réglé trois niveaux, un seul était persisté, et le retrait suivant
# révélait la vérité en faisant « disparaître » les autres.
#
# CE QU'IL PROTÈGE :
#   · la rafale doit devenir UN seul envoi (sinon on redéploie trois fois pour trois clics) ;
#   · un 409 doit être RÉESSAYÉ, jamais avalé ;
#   · `/state` répond l'ANCIENNE valeur pendant le redéploiement : reconstruire l'affichage
#     dessus ferait réapparaître ce qu'on vient de retirer, puis disparaître à nouveau ;
#   · un échec définitif doit RESYNCHRONISER l'écran sur ce qui est vraiment enregistré — laisser
#     une valeur optimiste est ce qui a rendu ce défaut si difficile à voir.
#
#   $ ./venv/bin/python tools/verif_hello_tally_rafale.py
import json
import re
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


# ⚠ CE BANC EST EN DEUX MOITIÉS, ET IL FAUT LES DEUX.
#   1. Une SIMULATION du contrat (regroupement, réessai sur 409, serveur avec sa garde) : elle
#      démontre que le contrat résout le défaut, mais elle REJOUE la logique au lieu de l'appeler
#      — monter la page entière demanderait un navigateur. Seule, elle prouverait la conception,
#      pas le code.
#   2. Des contrôles sur le CODE du plugin, qui vérifient qu'il implémente bien ce contrat.
# L'une sans l'autre laisserait passer soit un contrat qui ne marche pas, soit un plugin qui ne
# l'applique pas.
HARNAIS = """
let TEMPS = 0;                       // horloge virtuelle : le banc ne doit pas ATTENDRE
const FILE = [];
global.setTimeout = (fn, ms) => { const t = {fn, at: TEMPS + (ms||0), on: true};
  FILE.push(t); return t; };
global.clearTimeout = (t) => { if (t && typeof t === 'object') t.on = false; };
global.setInterval = () => 0; global.clearInterval = () => {};
function avancer(ms) {
  const fin = TEMPS + ms;
  while (true) {
    const p = FILE.filter(t => t.on && t.at <= fin).sort((a,b) => a.at - b.at)[0];
    if (!p) break;
    TEMPS = p.at; p.on = false; p.fn();
  }
  TEMPS = fin;
}
class El {
  constructor(t){ this.tag=t; this.children=[]; this.className=''; this._txt='';
    this.value=''; this.disabled=false; this.type=''; this.dataset={}; this.style={};
    this.onclick=null; this.onchange=null; }
  set textContent(v){ this._txt=v; if(v==='') this.children=[]; }
  get textContent(){ return this._txt; }
  appendChild(c){ this.children.push(c); return c; }
  setAttribute(){} addEventListener(){} removeEventListener(){}
  contains(){ return false; }
  get txt(){ return this._txt + this.children.map(c=>c.txt).join(''); }
  tous(cls){ let r=(this.className||'').split(' ').includes(cls)?[this]:[];
    for(const c of this.children) r=r.concat(c.tous?c.tous(cls):[]); return r; }
  querySelector(){ return null; } querySelectorAll(){ return []; }
}
const NOEUDS = {};
global.document = { createElement: t => new El(t), body: new El('body'),
  createTextNode: t => { const e = new El('#text'); e.textContent = t; return e; },
  getElementById: id => (NOEUDS[id] = NOEUDS[id] || new El('div')),
  querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {} };
global.window = {};

// ── LE SERVEUR, avec sa garde anti-rafale ───────────────────────────────────────────────────
const SERVEUR = { persiste: {tally_level: []}, enVol: false, recus: 0, refus: 0, deploiements: 0 };
global.fetch = async (url, opt) => {
  SERVEUR.recus++;
  if (SERVEUR.enVol) { SERVEUR.refus++; return {ok:false, status:409, json: async()=>({}) }; }
  const p = JSON.parse(opt.body).params;
  SERVEUR.enVol = true; SERVEUR.deploiements++;
  Object.assign(SERVEUR.persiste, p);
  // Le redéploiement dure : c'est LUI qui crée la fenêtre où tout se perdait.
  setTimeout(() => { SERVEUR.enVol = false; }, 1500);
  return {ok:true, status:200, json: async()=>({ok:true})};
};
"""


def executer_brut(src):
    """Exécute un source AUTONOME — sans le harnais ni le plugin entier. Le contrôle du gabarit
    a besoin d'un contexte NU : réinjecter tout le plugin y redéclarerait `esc`, `T`, `EL`."""
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(src)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        ligne = (r.stdout or "").strip().split("\n")[-1]
        return json.loads(ligne) if ligne.startswith("{") else {"exception": (r.stderr or "")[:300]}
    finally:
        os.unlink(f.name)


def executer(scenario):
    ctl = open(os.path.join(RACINE, "static", "js", "controls.js"), encoding="utf-8").read()
    plug = open(os.path.join(RACINE, "plugins", "hello_world", "control.js"),
                encoding="utf-8").read()
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(HARNAIS + ctl + "\n" + plug + "\n" + scenario)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        ligne = (r.stdout or "").strip().split("\n")[-1]
        return json.loads(ligne) if ligne.startswith("{") else {"exception": (r.stderr or "")[:300]}
    finally:
        os.unlink(f.name)


print("hello_world — trois niveaux choisis coup sur coup, puis un retrait\n")

# ── LA VRAIE RÉPONSE : ces réglages ne redéploient RIEN ──────────────────────
# ★ POURQUOI REDÉPLOYER POUR UN NIVEAU DE TALLY ? On ne devrait pas. Le niveau est lu par
# l'ORCHESTRATEUR (distributeur TSL, hook `tally_targets`), jamais par le conteneur. Redéployer
# pour ça coupe un flux vidéo pour changer une case — et comme le déploiement est sérialisé,
# c'est ce qui faisait perdre les gestes successifs en 409. Le regroupement et le réessai
# restent utiles pour les réglages qui, EUX, sont lus au démarrage (le format de sortie).
sys.path.insert(0, RACINE)
from app import plugins as _plg                                              # noqa: E402
_plg._scan()
_chaud = _plg.cles_sans_redeploiement("hello_world")
controle("★★★ le niveau de tally est déclaré SANS redéploiement",
         "tally_level" in _chaud,
         "il est lu par l'orchestrateur, jamais par le conteneur : le redéploiement était du "
         "travail perdu, et c'est lui qui déclenchait la garde anti-rafale")
controle("★★ le libellé de source aussi", "tally_label_col" in _chaud)

# ── LES PUCES NE SONT PAS DANS UN <label> ────────────────────────────────────
# Un <label> sans « for » renvoie TOUT clic vers le premier contrôle qu'il contient — ici la
# croix de la première puce. Cliquer à côté d'une puce supprimait donc le premier niveau.
# Le contrôle s'en défend lui-même (cf. verif_controle_choix), mais le balisage d'accueil doit
# être juste aussi : une garde n'excuse pas un contenant qui ment sur ce qu'il fait.
_js_pg = open(os.path.join(RACINE, "plugins", "hello_world", "control.js"),
              encoding="utf-8").read()
_avant_puces = _js_pg[:_js_pg.index('id="hw-tniv"')]
controle("★★★ le contenant des puces n'est pas un <label>",
         _avant_puces.rfind('<div class="hw-champ">') > _avant_puces.rfind("<label"),
         "un <label> convient à un champ unique, pas à une liste d'actions : il renvoie tout "
         "clic vers le premier bouton qu'il contient")

# ── OÙ CES RÉGLAGES S'AFFICHENT ──────────────────────────────────────────────
# ★ LE PANNEAU ⚙ EST POUR LE SYSTÈME, PAS POUR L'EXPLOITATION. Format de sortie, niveau de
# journal, mode tranche : ce qu'on règle en installant. Un niveau de tally et un libellé de
# source sont des gestes d'EXPLOITATION, qui se font sur la page du plugin, là où l'on voit ce
# qu'ils changent. Les mettre aux deux endroits, c'est deux écrans à tenir d'accord et deux
# chemins de droits pour un seul réglage.
_sys = _plg.config_scope_keys("hello_world", "system")
_usr = _plg.config_scope_keys("hello_world", "user")
controle("★★★ le tally n'est PAS dans le panneau ⚙ ni dans la palette",
         "tally_level" not in _sys and "tally_level" in _usr,
         "c'est un geste d'exploitation : sa place est sur la page du plugin. Obtenu "
         "system=%s user=%s" % (sorted(_sys), sorted(_usr)))
controle("★★ le libellé de source non plus", "tally_label_col" not in _sys)
controle("★★★ le format, lui, RESTE au panneau", "format" in _sys,
         "il se règle à l'installation et le conteneur le lit au démarrage")
controle("★★ ...avec le niveau de journal et le mode tranche",
         {"log_level", "slice_mode"} <= _sys,
         "obtenu %s" % sorted(_sys))
_src_panneau = open(os.path.join(RACINE, "templates", "plugin_section.html"),
                    encoding="utf-8").read()
controle("★★ le panneau ne rend QUE les champs de scope système",
         "(f.scope||'system')!=='user'" in _src_panneau.replace(" ", ""),
         "c'est ce filtre qui fait tenir la séparation ; le retirer ramènerait le tally dans "
         "un écran où il n'a rien à faire")
# ⚠ ET LE TRI SE FAIT SUR CE QUI CHANGE, PAS SUR CE QUI EST ENVOYÉ. Les écrans postent tout le
# formulaire : le panneau ⚙ envoie TOUS les champs `system`. Sans ce tri, cocher un niveau de
# tally embarquait `format` dans le lot et forçait un redéploiement — le chemin « à chaud » ne
# servait donc jamais depuis cet écran, et l'utilisateur ne voyait aucune différence.
from app.routes.plugin_registry import _cles_changees                        # noqa: E402
_p = {"format": "HD 1080i50", "tally_level": ["a"], "tally_label_col": 2}
controle("★★★ un champ renvoyé À L'IDENTIQUE ne compte pas comme un changement",
         _cles_changees("hello_world", dict(_p), _p) == set(),
         "sinon tout le formulaire compte comme modifié, et tout redéploie. Obtenu %r"
         % _cles_changees("hello_world", dict(_p), _p))
controle("★★★ dans un lot complet, seul le champ modifié est retenu",
         _cles_changees("hello_world", dict(_p, tally_level=["a", "b"]), _p) == {"tally_level"},
         "c'est ce tri qui rend le chemin à chaud atteignable depuis le panneau ⚙")
controle("★★ un vrai changement de format est bien vu",
         "format" in _cles_changees("hello_world", dict(_p, format="UHD 2160p50"), _p),
         "le rater ferait croire à un réglage appliqué qui n'aurait pas redéployé")

controle("★★★ le FORMAT, lui, redéploie bien", "format" not in _chaud,
         "le conteneur le lit à son démarrage : le déclarer à chaud donnerait un réglage qui "
         "semble pris et ne s'applique qu'au prochain déploiement, sans que rien ne le dise")
# ⚠ MESURE RÉELLE, PAS UNE INSPECTION DE SOURCE. Une première version cherchait le nom de la
# fonction avant la garde dans le fichier — et le trouvait ailleurs, donc elle acquiesçait même
# quand le court-circuit était retiré (vérifié par mutation). On ÉCRIT trois fois d'affilée sur
# le conteneur réel, ce qui est exactement le geste rapporté, et on RESTAURE la valeur.
try:
    import main                                                              # noqa: E402
    from app.database import get_db, db_get_containers, db_get_tally_levels  # noqa: E402
    from app.routes.plugin_registry import _load_dc                          # noqa: E402
    _app = main.app
    _app.config["TESTING"] = True
    with get_db() as _db:
        _u = _db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    _cli = _app.test_client()
    with _cli.session_transaction() as _s:
        _s["user_id"] = _u["id"]
        _s["username"] = _u["username"]
    _hw = [c for c in db_get_containers()
           if (_load_dc(c) or {}).get("type") == "hello_world" and c.get("status") == "running"]
except Exception as _e:
    _hw, _cli = [], None
    print("  (chemin réel non mesurable : %s)" % _e)

if _hw:
    _v = _hw[0]["vmid"]
    _lu = lambda: (_load_dc([c for c in db_get_containers() if c["vmid"] == _v][0])
                   or {}).get("params", {}).get("tally_level")
    _avant = _lu()
    _niv = [n["uuid"] for n in db_get_tally_levels()]
    try:
        _codes = []
        for _i in range(1, min(4, len(_niv) + 1)):
            _r = _cli.post("/api/containers/%d/plugin_config" % _v,
                           json={"params": {"tally_level": _niv[:_i]}})
            _codes.append((_r.status_code, (_r.get_json() or {}).get("redeploye")))
        controle("★★★ trois écritures d'affilée sur un conteneur QUI TOURNE : aucune refusée",
                 all(c == 200 for c, _ in _codes),
                 "c'est le geste rapporté. Un 409 ici, c'est un niveau perdu en silence. "
                 "Obtenu %r" % (_codes,))
        controle("★★★ ...et AUCUN redéploiement",
                 all(d is False for _, d in _codes),
                 "redéployer pour un niveau de tally coupe un flux vidéo pour changer une case, "
                 "et c'est ce qui déclenchait la garde anti-rafale. Obtenu %r" % (_codes,))
        # ── ⚠ `/state` EST UNE PHOTO, PAS UN MIROIR ─────────────────
        # Le conteneur répond avec la configuration qui lui a été remise à SON DÉPLOIEMENT.
        # Tant qu'un réglage redéployait, la photo restait fraîche. Depuis qu'un réglage
        # s'applique à chaud, elle ne l'est plus : la page se reconstruisait sur l'ancienne
        # liste et défaisait le geste — « j'ai supprimé le 2, ça a supprimé le 3 aussi ».
        _rp = _cli.get("/api/containers/%d/plugin_config" % _v)
        controle("★★★ l'orchestrateur sert les réglages PERSISTÉS", _rp.status_code == 200,
                 "sans cette route, la page n'a que la photo du conteneur pour se relire")
        controle("★★★ et c'est la valeur À JOUR, pas celle du déploiement",
                 ((_rp.get_json() or {}).get("params") or {}).get("tally_level") == _lu(),
                 "c'est LA correction : le conteneur ne se sert pas de ce réglage, il n'a "
                 "aucune raison d'en être la référence. Obtenu %r"
                 % ((_rp.get_json() or {}).get("params") or {}).get("tally_level"))
        _src_pg = open(os.path.join(RACINE, "plugins", "hello_world", "control.js"),
                       encoding="utf-8").read()
        # ⚠ ON EXIGE QUE LA VALEUR PERSISTÉE PRIME, pas seulement qu'elle soit lue. Une
        # première version se contentait de chercher le nom de la variable : la remplacer par
        # `s.tally_level` gardait le nom et le contrôle acquiesçait (vérifié par mutation).
        _prime = ('("tally_level" in _persiste) ? _persiste.tally_level'
                  in _src_pg.replace("\n", " ").replace("  ", " "))
        controle("★★★ la valeur PERSISTÉE prime sur celle de `/state`",
                 _prime,
                 "lire la route ne suffit pas : il faut s'en servir. Sinon la page se "
                 "reconstruit sur la photo du conteneur et défait le geste")
        controle("★★ ...et la route est bien appelée au montage",
                 "chargerPersistes" in _src_pg and "_persiste[cle] = valeur" in _src_pg,
                 "elle doit aussi retenir ce que l'orchestrateur vient d'accepter : `/state` "
                 "mettra un déploiement à le rattraper, ou ne le rattrapera jamais")
        controle("★★ ...en le SAUTANT en mode public, où il n'y a pas d'orchestrateur",
                 "if (!url) return;" in _src_pg,
                 "un appel hors relais échoue derrière un jeton : il se saute, il ne se tente pas")

        controle("★★★ la dernière valeur écrite est bien celle qui reste",
                 _lu() == _niv[:min(3, len(_niv))],
                 "attendu %r, obtenu %r" % (_niv[:3], _lu()))
    finally:
        # ⚠ ON VÉRIFIE LA RESTAURATION, et on crie si elle n'a pas pris. Ce banc écrit sur un
        # conteneur RÉEL de l'exploitant : une restauration qu'on suppose réussie laisse dériver
        # son réglage d'une exécution à l'autre — c'est arrivé pendant une campagne de mutation,
        # où les écritures partaient en 409 et où le « avant » lu au départ n'était déjà plus le
        # sien.
        _cli.post("/api/containers/%d/plugin_config" % _v,
                  json={"params": {"tally_level": _avant or []}})
        _rendu = _lu()
        _ok = (_rendu or []) == (_avant or [])
        print("  (réglage du conteneur %d %s : %r)"
              % (_v, "restauré" if _ok else "NON RESTAURÉ ⚠", _rendu))
        if not _ok:
            echecs.append("restauration du conteneur")
            print("        → attendu %r — à remettre à la main" % (_avant,))
else:
    print("  (aucun conteneur hello_world en marche : le chemin réel n'est pas mesuré)")


res = executer("""
const out = {};
try {
  // ⚠ ON NE MONTE PAS LA PAGE ENTIÈRE. `mount()` accroche des écouteurs sur des nœuds qu'un faux
  // DOM ne peut pas fournir sans devenir un navigateur — et un bouchon qui les fabrique ferait
  // passer le contrôle sans rien prouver. On vérifie ce qui est vérifiable ici : le fichier se
  // charge et s'enregistre. Le RENDU de la page, lui, est EXÉCUTÉ plus bas — `node --check` ne
  // suffit pas : un accent grave dans un commentaire du gabarit ferme la chaîne sans rendre le
  // fichier invalide (cf. `services/tsl/settings_tab.html`, 2026-09-01).
  out.enregistre = !!(window.MXLPlugins && window.MXLPlugins.hello_world
                      && typeof window.MXLPlugins.hello_world.mount === 'function');
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
""")
controle("★ le module du plugin se charge et s'enregistre",
         res.get("enregistre") and not res.get("exception"),
         "obtenu %r" % (res.get("exception") or res.get("enregistre")))

# ── Le cœur : la rafale ──────────────────────────────────────────────────────
res = executer("""
const out = {};
try {
  // On exerce la logique d'envoi telle qu'elle est écrite dans le plugin, en la rejouant sur le
  // même contrat : trois gestes rapprochés, puis on laisse le temps passer.
  const src = require('fs').readFileSync(process.argv[1], 'utf8');
  out.aDebounce = /_enAttente\\[cle\\] = setTimeout/.test(src);
  out.aRetry409 = /r\\.status === 409 && reste > 0/.test(src);
  out.aGardeEtat = /_ecritureEnCours\\.has\\("tally_level"\\)/.test(src);
  out.aResync = /delete n\\.dataset\\.sig/.test(src);
  out.drapeauModule = /^  const _ecritureEnCours = new Set\\(\\);$/m.test(src);
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
""")
controle("★★★ les gestes sont GROUPÉS avant l'envoi", res.get("aDebounce"),
         "trois clics = trois redéploiements, et deux POST refusés en 409")
controle("★★★ un 409 est RÉESSAYÉ, pas avalé", res.get("aRetry409"),
         "c'est le refus qui faisait perdre les niveaux, en silence côté écran")
controle("★★★ l'affichage ne se reconstruit pas pendant une écriture",
         res.get("aGardeEtat"),
         "`/state` répond l'ANCIENNE valeur pendant le redéploiement : reconstruire dessus fait "
         "réapparaître ce qu'on vient de retirer, puis disparaître à nouveau")
controle("★★ un échec définitif RESYNCHRONISE l'écran", res.get("aResync"),
         "laisser une valeur optimiste est ce qui a rendu ce défaut invisible")
controle("★★★ le drapeau est au niveau MODULE, pas dans une fonction",
         res.get("drapeauModule"),
         "il est posé dans `gabarit()` et lu dans `rendreEtat()` : deux fonctions sœurs. Un "
         "`const` local n'existe pas dans l'autre — la référence LÈVE, et toute la mise à jour "
         "d'état tombe. `node --check` n'y voit rien")

# ── La simulation complète : trois ajouts, un retrait ────────────────────────
res = executer("""
const out = {};
try {
  const hote = new El('div');
  const OPTS = [{value:'n1',label:'1'},{value:'n2',label:'2'},{value:'n3',label:'3'}];
  // On rejoue le contrat exact du plugin : chooseList + envoi groupé + réessai sur 409.
  const ATTENTE_MS = 500, ESSAIS = 6;
  const enAttente = {}; const enCours = new Set();
  const poster = async (cle, val, reste) => {
    enCours.add(cle);
    const r = await fetch('/x', {body: JSON.stringify({params:{[cle]: val}})});
    if (r.status === 409 && reste > 0) { setTimeout(() => poster(cle, val, reste-1), 700); return; }
    enCours.delete(cle);
  };
  const envoyer = (cle, val) => {
    clearTimeout(enAttente[cle]); enCours.add(cle);
    enAttente[cle] = setTimeout(() => { delete enAttente[cle]; poster(cle, val, ESSAIS); },
                                ATTENTE_MS);
  };
  const api = window.MXLControls.chooseList(hote, {
    options: OPTS, valeurs: [], onChange: v => envoyer('tally_level', v) });
  const sel = () => hote.tous('ctl-choix-select')[0];
  const puces = () => hote.tous('ctl-choix-puce');
  // Trois ajouts coup sur coup (200 ms d'écart : plus rapide que le regroupement).
  sel().value='n1'; sel().onchange(); avancer(200);
  sel().value='n2'; sel().onchange(); avancer(200);
  sel().value='n3'; sel().onchange(); avancer(200);
  out.puces3 = puces().length;
  avancer(6000);
  out.persisteApresAjouts = SERVEUR.persiste.tally_level;
  out.deploiements = SERVEUR.deploiements;
  out.refus = SERVEUR.refus;
  // Puis on retire le PREMIER.
  puces()[0].tous('ctl-choix-x')[0].onclick();
  avancer(6000);
  out.pucesApres = puces().length;
  out.persisteApresRetrait = SERVEUR.persiste.tally_level;
} catch (e) { out.exception = e.message; }
console.log(JSON.stringify(out));
""")
controle("★ trois puces à l'écran après trois choix", res.get("puces3") == 3,
         "obtenu %r (%r)" % (res.get("puces3"), res.get("exception")))
controle("★★★ les TROIS niveaux sont réellement persistés",
         res.get("persisteApresAjouts") == ["n1", "n2", "n3"],
         "c'est LE défaut : deux POST partaient en 409 et on gardait un seul niveau, avec trois "
         "puces à l'écran. Obtenu %r" % (res.get("persisteApresAjouts"),))
controle("★★★ trois clics = UN seul déploiement", res.get("deploiements") == 1,
         "redéployer un conteneur à chaque clic d'une sélection multiple est ce qui déclenchait "
         "la garde anti-rafale. Obtenu %r déploiement(s)" % res.get("deploiements"))
controle("★★ retirer le premier ne retire QUE le premier",
         res.get("pucesApres") == 2 and res.get("persisteApresRetrait") == ["n2", "n3"],
         "écran %r, enregistré %r" % (res.get("pucesApres"), res.get("persisteApresRetrait")))

# ── LE GABARIT DE LA PAGE S'EXÉCUTE ─────────────────────────────────────────
# `node --check` est AVEUGLE à l'accident le plus probable ici : un accent grave dans un
# commentaire HTML situé DANS le gabarit ferme la chaîne, le fichier reste valide, et la page
# se vide à l'exécution sans un message nulle part. Seul le rendu le voit. On stube le strict
# nécessaire — un bouchon qui en ferait plus ferait passer le contrôle sans rien prouver.
JS = _js_pg
_d = JS.index("  function gabarit(")
_m = re.search(r"\n  (?:function |const |async function )", JS[_d + 20:])
res = executer_brut(
    "const esc = s => String(s==null?'':s);\n"
    "const T = (k, r) => (r === undefined ? k : r);\n"
    "const EL = {innerHTML:'', querySelectorAll: () => [], querySelector: () => null};\n"
    "const knob = () => '<knob>'; const PUBLIC = false; const $ = () => null;\n"
    "global.window = {};\n"
    "window.MXLControls = {knobSvg: () => '<svg>', ICONS: new Proxy({}, {get: () => '<i>'})};\n"
    + JS[_d:_d + 20 + _m.start()]
    # `gabarit()` accroche ses écouteurs APRÈS avoir posé l'innerHTML : l'erreur qu'on avale ici
    # est celle du faux DOM, pas celle du gabarit — s'il avait levé, `EL.innerHTML` serait vide.
    + "\nlet out_err = null;\ntry { gabarit(); } catch (e) { out_err = e.message; }\n"
      "const h = EL.innerHTML;\n"
      "console.log(JSON.stringify({len: h.length,\n"
      "  titres: (h.match(/hw-sec-t\">[^<]*/g) || []).map(x => x.split('>')[1])}));\n")

controle("★★★ le gabarit de la page S'EXÉCUTE", (res.get("len") or 0) > 3000,
         "un gabarit dont la chaîne est fermée par un accent grave rend une page VIDE, en "
         "restant syntaxiquement valide. Obtenu %r caractères" % res.get("len"))
controle("★★ on nomme le SUJET, pas le mécanisme",
         "Format de sortie" in (res.get("titres") or [])
         and "Labels & Tally" in (res.get("titres") or [])
         and not any("redéploie" in t or "à chaud" in t for t in (res.get("titres") or [])),
         "un titre qui annonce la CONSÉQUENCE d'y toucher (« Réglage qui redéploie », "
         "« Réglages appliqués à chaud ») n'aide personne à trouver ce qu'il cherche, et le "
         "second était faux — d'autres réglages de la page s'appliquent aussi à chaud. "
         "Obtenu %r" % (res.get("titres"),))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
