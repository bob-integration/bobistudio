// Banc du TÉMOIN DE CONNEXION (window.MXLConnexion, templates/layout.html).
// À lancer à la main :  node tools/verif_temoin_connexion.js
//
// Cas couverts : échec franc, incident ponctuel (« blip ») dont la sonde réussit, 401 (session
// expirée, PAS une coupure), et annulation de navigation (AbortError, PAS une coupure non plus).
// Le cas « la requête PEND », qui est le vrai piège, a son propre banc : verif_temoin_connexion_pendu.js
//
// Le module est EXTRAIT de layout.html À L'EXÉCUTION : un banc qui embarquerait une copie
// finirait par valider du code que plus personne ne sert.
// Le bouchon DOM émule `textContent = ''` qui vide `innerHTML` — sans quoi on mesurerait une
// limite du bouchon et pas le comportement du code.

const fs = require('fs'), path = require('path');

let cls = new Set();
let el = { id:'', className:'', innerHTML:'', setAttribute(){},
           set textContent(v){ this.innerHTML = v; }, get textContent(){ return this.innerHTML; } };
global.document = {
  body:{ classList:{ add:(...a)=>a.forEach(x=>cls.add(x)), remove:(...a)=>a.forEach(x=>cls.delete(x)) }, appendChild(){} },
  getElementById: () => el.id ? el : null,
  createElement: () => { el.id='mxl-connexion'; return el; },
  documentElement:{}, addEventListener(){}, hidden:false,
};
global.window = { t:k=>k, addEventListener:()=>{}, fetch:null, AbortController: global.AbortController };
window.setTimeout = global.setTimeout; window.setInterval = global.setInterval;

let mode = 'ok';   // ok | blip | reseau | 401 | abort
window.fetch = async (url) => {
  // « blip » = incident ponctuel : l'appel applicatif échoue mais le serveur est là et répond
  // à la sonde. C'est le cas qui ne DOIT PAS déclencher d'alarme.
  if (mode === 'blip') {
    if (url === '/api/ping') return { ok:true, status:200 };
    const e = new Error('blip'); e.name = 'TypeError'; throw e;
  }
  if (mode === 'reseau') { const e = new Error('Failed to fetch'); e.name='TypeError'; throw e; }
  if (mode === 'abort')  { const e = new Error('aborted');        e.name='AbortError'; throw e; }
  if (mode === '401')    return { ok:false, status:401 };
  return { ok:true, status:200 };
};

// ── extraction du module depuis layout.html ──────────────────────────────────────────────────
const src = fs.readFileSync(path.join(__dirname, '..', 'templates', 'layout.html'), 'utf8');
const _d = src.indexOf('window.MXLConnexion = (function ()');
const _f = src.indexOf('})();', src.indexOf('return {', _d)) + '})();'.length;
if (_d < 0 || _f < _d) { console.error("MXLConnexion introuvable dans layout.html"); process.exit(2); }
eval(src.slice(_d, _f));

const sleep = ms => new Promise(r => setTimeout(r, ms));
function etat() { return { perdue: window.MXLConnexion.perdue(), classes:[...cls], texte:(el.innerHTML||'').replace(/<[^>]*>/g,'|') }; }
(async () => {
  let ko = 0; const v = (c, m) => { console.log((c?'  ✓ ':'  ✗ ') + m); if (!c) ko++; };

  await window.fetch('/api/x');
  v(!etat().perdue, "marche normale → aucun bandeau");

  mode = 'abort';
  try { await window.fetch('/api/x'); } catch(e) {}
  try { await window.fetch('/api/x'); } catch(e) {}
  v(!etat().perdue, "AbortError (navigation) → ignoré, pas de fausse alerte");

  mode = 'blip';
  try { await window.fetch('/api/x'); } catch(e) {}
  await sleep(30);
  v(!etat().perdue, "blip isolé (appel KO mais sonde OK) → pas d'alarme");

  mode = 'reseau';
  try { await window.fetch('/api/x'); } catch(e) {}
  await sleep(30);
  v(etat().perdue, "échec réseau CONFIRMÉ par la sonde → connexion déclarée perdue");
  v(cls.has('mxl-hors-ligne') && cls.has('home-stale'), "voile « périmé » posé sur la page");
  v(/conn.perdue/.test(etat().texte), "bandeau « connexion perdue »");
  v(/conn.constatee/.test(etat().texte) && /\d{1,2}[:h]\d{2}/.test(el.innerHTML),
    "l'HEURE du constat est affichée (et non un chronomètre)");
  const fige = el.innerHTML;
  await sleep(1200);
  v(el.innerHTML === fige, "le texte ne bouge plus (heure figée, pas de compteur qui défile)");

  mode = 'ok';
  await window.fetch('/api/x'); await sleep(30);
  v(!etat().perdue, "retour du réseau → connexion rétablie");
  v(!cls.has('mxl-hors-ligne'), "voile retiré");
  v(/conn.retablie/.test(etat().texte), "bandeau « rétablie »");

  mode = '401';
  await window.fetch('/api/x'); await sleep(30);
  v(!etat().perdue, "401 → PAS traité comme une coupure réseau");
  v(/conn.session/.test(etat().texte), "bandeau distinct « session expirée »");

  mode = 'ok'; await window.fetch('/api/x'); await sleep(30);
  v(!/conn.session/.test(etat().texte), "session revenue → bandeau retiré");

  console.log(ko ? `\n${ko} test(s) EN ÉCHEC` : "\nTous les tests passent");
  process.exit(ko ? 1 : 0);
})();
