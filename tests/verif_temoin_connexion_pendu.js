// Banc du TÉMOIN DE CONNEXION — CAS DE LA REQUÊTE QUI PEND.
// À lancer à la main :  node tools/verif_temoin_connexion_pendu.js
//
// C'est le cas qui a motivé ce banc, et il est contre-intuitif : quand la liaison disparaît
// (VPN coupé, wifi perdu), `fetch` ne REJETTE PAS — il PEND, jusqu'à l'expiration TCP, qui se
// compte en minutes. Aucune erreur n'est levée, aucun événement n'est émis. Une première version
// du témoin n'écoutait que les rejets : elle ne voyait rien pendant tout ce temps.
// Pire : `MXLPoll` n'ordonnance la passe suivante qu'à la fin de la précédente, donc une requête
// pendue ARRÊTE tous les polls de la page — le mécanisme de détection était neutralisé par la
// panne qu'il devait détecter.
// On vérifie ici que le SILENCE suffit à trancher, sans qu'aucune erreur ne soit jamais levée.
//
// Le module est EXTRAIT de layout.html à l'exécution (pas de copie qui se périme).

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

let mode = 'ok';
window.fetch = (url, opts) => {
  if (mode === 'ok') return Promise.resolve({ ok:true, status:200 });
  // … pend indéfiniment. Ne se dénoue QUE si l'appelant annule (ce que fait la sonde,
  // seul appel à porter un délai de garde).
  return new Promise((_res, rej) => {
    const sig = opts && opts.signal;
    if (sig) sig.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; rej(e); });
  });
};

const src = fs.readFileSync(path.join(__dirname, '..', 'templates', 'layout.html'), 'utf8');
const _d = src.indexOf('window.MXLConnexion = (function ()');
const _f = src.indexOf('})();', src.indexOf('return {', _d)) + '})();'.length;
if (_d < 0 || _f < _d) { console.error("MXLConnexion introuvable dans layout.html"); process.exit(2); }
eval(src.slice(_d, _f));

const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  let ko = 0; const v = (c, m) => { console.log((c?'  ✓ ':'  ✗ ') + m); if (!c) ko++; };
  const t0 = Date.now();

  await window.fetch('/api/x');
  v(!window.MXLConnexion.perdue(), "marche normale → pas de bandeau");

  mode = 'pend';                                     // ← la liaison disparaît
  window.fetch('/api/home/summary').catch(()=>{});   // un poll part et NE REVIENT JAMAIS
  v(!window.MXLConnexion.perdue(), "juste après la coupure → rien encore (normal)");

  await sleep(12000);
  const detecte = window.MXLConnexion.perdue();
  const delai = window.MXLConnexion.depuis() ? (window.MXLConnexion.depuis() - t0) / 1000 : null;
  v(detecte, `coupure DÉTECTÉE malgré une requête pendue (à t+${delai}s)`);
  v(delai !== null && delai < 12, "détectée en moins de 12 s (aucune erreur n'a jamais été levée)");
  v(cls.has('mxl-hors-ligne'), "voile « périmé » posé");
  v(/conn.perdue/.test(el.innerHTML), "bandeau « connexion perdue » affiché");
  v(/conn.constatee/.test(el.innerHTML), "heure du constat affichée");

  mode = 'ok';                                       // ← liaison rétablie
  await sleep(4000);
  v(!window.MXLConnexion.perdue(), "reprise automatique, sans action de l'utilisateur");
  v(!cls.has('mxl-hors-ligne'), "voile retiré");

  console.log(ko ? `\n${ko} test(s) EN ÉCHEC` : "\nTous les tests passent");
  process.exit(ko ? 1 : 0);
})();
