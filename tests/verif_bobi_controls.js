#!/usr/bin/env node
// Logique PURE de static/js/bobi-controls.js : échelles valeur↔position, loi dB, quantification,
// pas clavier, ballistique du vumètre, émetteur throttlé, mapping feuille param_tree → contrôle.
// Aucun DOM, aucun réseau. Lancé par la CI avec les autres tests/*.js.
//
//   node tests/verif_bobi_controls.js

const path = require('path');
const BC = require(path.join(__dirname, '..', 'static', 'js', 'bobi-controls.js'));
let echecs = 0;
function ok(cond, msg) { if (!cond) { echecs++; console.log('  ✗ ' + msg); } else console.log('  ✓ ' + msg); }
const near = (a, b, e) => Math.abs(a - b) <= (e || 1e-6);

// ─── Loi de fader dB ───────────────────────────────────────────────────────────────────
const db = BC.makeScale({ min: -Infinity, max: 10, scale: 'db', step: 0.5, default: 0 });
ok(db.kind === 'db', 'échelle dB reconnue');
ok(near(db.toPos(0), 0.75, 1e-9), 'unité (0 dB) à 75 % de course');
ok(db.toPos(-Infinity) === 0 && db.fromPos(0) === -Infinity, '−∞ en bas de course');
ok(db.toPos(10) === 1 && db.fromPos(1) === 10, '+10 dB en haut de course');
let mono = true, prev = -Infinity;
for (let i = 0; i <= 1000; i++) { const v = db.fromPos(i / 1000); if (v < prev) mono = false; prev = v; }
ok(mono, 'fromPos monotone croissante sur 1001 points');
let monoP = true, pp = -1;
for (let v = -90; v <= 10; v += 0.25) { const p = db.toPos(v); if (p < pp) monoP = false; pp = p; }
ok(monoP, 'toPos monotone croissante');
let rt = true;
for (let v = -80; v <= 10; v += 0.5) { if (!near(db.fromPos(db.toPos(v)), v, 1e-6)) rt = false; }
ok(rt, 'aller-retour valeur→position→valeur exact (−80…+10 dB)');
ok(db.quantize(db.fromPos(0.7501)) === 0, 'quantification : 0 dB exact autour de l\'unité');
ok(db.quantize(-3.26) === -3.5 && db.quantize(-3.24) === -3, 'quantification au pas de 0,5 dB');
ok(db.quantize(99) === 10 && db.quantize(-Infinity) === -Infinity, 'bornes : +10 et −∞ conservés');
ok(db.default === 0, 'défaut dB = unité');
ok(db.nudge(-Infinity, +1) === -90 && db.nudge(-89.8, -1) === -Infinity, 'pas clavier : −∞ ↔ plancher −90');
ok(db.nudge(9.8, +1) === 10, 'pas clavier borné à max');

const dbf = BC.makeScale({ min: -60, max: 6, scale: 'db' });
ok(dbf.toPos(-60) === 0 && dbf.toPos(6) === 1 && dbf.fromPos(0) === -60, 'dB à bornes finies : course renormalisée');
ok(dbf.fromPos(dbf.toPos(0)) !== undefined && near(dbf.fromPos(dbf.toPos(0)), 0, 1e-9), 'dB à bornes finies : aller-retour à 0 dB');
ok(BC.makeScale({ min: 0, max: 10, scale: 'db' }).kind === 'linear', 'dB sans plage encadrant 0 : repli linéaire');
ok(BC.makeScale({ min: null, max: 10, scale: 'db' }).min === -Infinity, 'borne null (JSON) = −∞');
ok(BC.makeScale({ min: '-inf', max: 10, scale: 'db' }).min === -Infinity, 'borne "-inf" = −∞');

// ─── Linéaire ──────────────────────────────────────────────────────────────────────────
const lin = BC.makeScale({ min: 0, max: 200, step: 5, default: 100 });
ok(lin.toPos(100) === 0.5 && lin.fromPos(0.5) === 100, 'linéaire : milieu');
ok(lin.quantize(102.4) === 100 && lin.quantize(102.6) === 105, 'linéaire : quantification au pas');
ok(lin.fromPos(-1) === 0 && lin.fromPos(2) === 200, 'linéaire : positions hors course bornées');
ok(lin.default === 100, 'linéaire : défaut');
ok(BC.makeScale({ min: -1, max: 1 }).default === -1, 'défaut implicite = min');
const lf = BC.makeScale({ min: 0, max: 1, step: 0.1 });
ok(lf.quantize(0.30000000000000004) === 0.3 && lf.nudge(0.2, +1) === 0.3, 'pas décimal sans bruit flottant');
ok(lin.toPos(NaN) === 0, 'NaN tolérée');

// ─── Affichage ─────────────────────────────────────────────────────────────────────────
ok(BC.fmtNumber(-Infinity, { unit: 'dB' }) === '−∞ dB', 'format −∞ dB');
ok(BC.fmtNumber(-6, { unit: 'dB', step: 0.5 }) === '−6.0 dB', 'format dB avec décimales du pas');
ok(BC.fmtNumber(45, { unit: '°' }) === '45°', 'format sans espace pour °');

// ─── Ballistique du vumètre ────────────────────────────────────────────────────────────
const st = BC.meterState();
BC.meterStep(st, { peak: -6, rms: -12 }, 0.1, 1000, {});
ok(st.peak === -6 && st.rms === -12 && st.hold === -6, 'montée immédiate, maintien posé');
BC.meterStep(st, { peak: -60, rms: -60 }, 0.5, 1500, { fall: 24 });
ok(near(st.peak, -18) && near(st.rms, -24), 'chute à 24 dB/s');
ok(st.hold === -6, 'maintien tenu avant l\'échéance');
BC.meterStep(st, { peak: -60, rms: -60 }, 0.5, 3000, { holdMs: 1500, holdFall: 12 });
ok(st.hold < -6, 'maintien relâché après holdMs');
ok(BC.meterStep(BC.meterState(), { peak: 0.5 }, 0, 0, {}).clip === true, 'saturation verrouillée');
const s2 = BC.meterState(); BC.meterStep(s2, { peak: -20, peakHold: -3 }, 0, 0, {});
ok(s2.hold === -3, 'peakHold fourni prime sur le calcul interne');
ok(BC.meterStep(BC.meterState(), { peak: NaN }, 0, 0, {}).peak === -Infinity, 'NaN → −∞');
ok(BC.meterPos(-30, -60, 0) === 0.5 && BC.meterPos(-Infinity, -60, 0) === 0 && BC.meterPos(6, -60, 0) === 1, 'position dBFS');

// ─── Émetteur : throttle rAF ───────────────────────────────────────────────────────────
{
    const q = []; let n = 0;
    const em = BC.createEmitter((f) => { q.push(f); return ++n; }, () => { q.length = 0; }, {});
    const got = [], chg = [];
    em.on('input', (v) => got.push(v)); em.on('change', (v) => chg.push(v));
    for (let i = 1; i <= 50; i++) em.queueInput(i);
    ok(q.length === 1 && got.length === 0, '50 mises à jour → 1 seule image planifiée');
    q.shift()();
    ok(got.length === 1 && got[0] === 50, 'un seul input, dernière valeur');
    em.queueInput(7); em.commit(7);
    ok(got[got.length - 1] === 7 && chg[0] === 7 && q.length === 0, 'commit purge l\'input en attente puis émet change');
    const off = em.on('change', () => { throw new Error('ne doit pas arriver'); }); off();
    em.discrete(9);
    ok(chg[chg.length - 1] === 9, 'discrete : change immédiat, désabonnement honoré');
    let bad = false; try { em.on('nope', () => {}); } catch (e) { bad = true; }
    ok(bad, 'événement inconnu refusé');
}

// ─── Feuille param_tree → contrôle ─────────────────────────────────────────────────────
const L = (o) => BC.leafToSpec(Object.assign({ key: 'k', label: 'L', endpoint: '/e', path: 'a.b' }, o));
let s;
s = L({ type: 'bool', default: false }); ok(s.kind === 'toggle' && s.opts.mode === 'latch', 'bool → bouton verrouillé');
s = L({ type: 'bool', mode: 'momentary' }); ok(s.opts.mode === 'momentary', 'bool momentané (ordre)');
s = L({ type: 'enum', options: ['a', 'b'] }); ok(s.kind === 'select' && s.opts.options.length === 2, 'enum → liste');
s = L({ type: 'enum', options: ['a'], widget: 'list' }); ok(s.opts.variant === 'list', 'enum + widget list');
ok(L({ type: 'text' }) === null, 'text → pas de contrôle (null)');
s = L({ type: 'number', unit: 'dB', min: null, max: 10 }); ok(s.kind === 'fader' && s.opts.scale === 'db' && s.opts.orientation === 'vertical', 'unité dB → fader vertical, loi dB');
s = L({ type: 'angle', min: 0, max: 360 }); ok(s.kind === 'knob' && s.opts.unit === '°', 'angle → rotatif °');
s = L({ type: 'percent', min: 0, max: 100 }); ok(s.kind === 'fader' && s.opts.orientation === 'horizontal' && s.opts.unit === '%', 'percent → fader horizontal %');
s = L({ type: 'position', min: 0, max: 1, widget: 'knob' }); ok(s.kind === 'knob', 'widget knob prime');
s = L({ type: 'duration', min: 0, max: 5000, step: 10 }); ok(s.opts.unit === 'ms' && s.opts.step === 10, 'durée en ms, pas conservé');
s = L({ type: 'number', min: 0, max: 1, widget: 'readout' }); ok(s.kind === 'readout', 'widget readout');
ok(s.opts.target.endpoint === '/e' && s.opts.target.path === 'a.b' && s.opts.label === 'L', 'cible (endpoint/path) et libellé transmis');
ok(BC.leafToSpec(null) === null, 'feuille nulle tolérée');
s = L({ type: 'set', options: ['pgm', 'g1', 'g2'] }); ok(s.kind === 'buttons' && s.opts.multiple === true && s.opts.options.length === 3, 'set → rangée de petits boutons multiples (affectation)');
s = L({ type: 'enum', widget: 'buttons', options: ['pre', 'post'] }); ok(s.kind === 'buttons' && !s.opts.multiple, 'enum widget buttons → petits boutons exclusifs (Pré/Post)');
s = L({ type: 'enum', options: ['a', 'b'] }); ok(s.kind === 'select', 'enum sans widget → reste une liste');
s = L({ type: 'bool', widget: 'small' }); ok(s.kind === 'toggle' && s.opts.size === 'small', 'bool widget small → petit bouton');
{
    const got = [];
    const em = BC.createEmitter((f) => f(), () => {}, {});
    em.on('push', (v) => got.push(['push', v])); em.on('change', (v) => got.push(['change', v]));
    em.push(true); em.discrete(3);
    ok(JSON.stringify(got) === '[["push",true],["change",3]]', 'émetteur : canal « push » séparé de la valeur');
}

// ─── Échelle log (fréquences), pas fin, grandeurs entières ──────────────────────────────
{
    const f = BC.makeScale({ min: 10, max: 20000, scale: 'log' });
    ok(f.kind === 'log', 'échelle log reconnue');
    ok(near(f.toPos(10), 0) && near(f.toPos(20000), 1), 'log : bornes aux deux bouts');
    ok(near(f.toPos(Math.sqrt(10 * 20000)), 0.5, 1e-9), 'log : moyenne géométrique au milieu de la course');
    const px = (v) => (f.fromPos(f.toPos(v) + 1 / 160) - v);     // hertz par pixel sur 160 px
    ok(px(80) < 4 && px(8000) > 300, 'log : ~3,8 Hz/px à 80 Hz, des centaines à 8 kHz (linéaire : 125 Hz/px partout) — ' + px(80).toFixed(2) + ' / ' + px(8000).toFixed(0));
    ok(f.quantize(83.456, f.step) === 83.5 && f.quantize(83.456, f.fineStep) === 83.46, 'log : grille 3 chiffres significatifs, 4 en fin');
    ok(f.nudge(80, +1, f.fineStep) > 80 && f.nudge(80, +1, f.fineStep) < 80.5, 'log : flèche en fin < 0,5 Hz à 80 Hz (' + f.nudge(80, +1, f.fineStep) + ')');
    ok(BC.fmtNumber(1234, { scale: 'log', unit: 'Hz' }) === '1.234 kHz' && BC.fmtNumber(80, { scale: 'log', unit: 'Hz' }) === '80 Hz', 'log : affichage 80 Hz / 1.234 kHz');
    const lin = BC.makeScale({ min: 20, max: 400, step: 5 });
    ok(lin.fineStep === 0.5, 'linéaire : pas fin = pas / 10');
    const ent = BC.makeScale({ min: 0, max: 1079, step: 1, integer: true });
    ok(ent.fineStep === 1 && ent.quantize(540.3, ent.fineStep) === 540, 'entier : le fin n\'invente pas de demi-ligne');
    s = L({ type: 'number', unit: 'Hz', min: 10, max: 20000 }); ok(s.opts.scale === 'log', 'feuille en Hz sur ≥ 1 décade → échelle log');
    s = L({ type: 'int', min: 0, max: 1079 }); ok(s.opts.integer === true, 'feuille int → grandeur entière');
}

// ─── Tracé du rotatif (unique, partagé avec le catalogue) ───────────────────────────────
{
    const svgIdx = BC.knobSvg('index', 0.75, 0.5, undefined, 0.5);
    ok(/class="ctl-knob-fill"/.test(svgIdx) && /class="ctl-knob-ptr"/.test(svgIdx), 'index : arc de valeur + index');
    ok(!/ctl-knob-seg/.test(svgIdx), 'index sans poussoir : aucun témoin');
    ok(/ctl-knob-seg on/.test(BC.knobSvg('index', 0.5, null, true)) && /ctl-knob-seg"/.test(BC.knobSvg('index', 0.5, null, false)), 'index à poussoir : témoin allumé / éteint');
    ok(/ctl-knob-seg on/.test(BC.knobSvg('gate', 0.5, null, undefined)), 'gate sans état : témoin allumé (historique)');
    ok(/ctl-knob-def on/.test(BC.knobSvg('arc', 0.5, 0.5)) && /ctl-knob-def"/.test(BC.knobSvg('arc', 0.7, 0.5)), 'repère de défaut accentué seulement sur le défaut');
    ok(!/ctl-knob-def/.test(BC.knobSvg('needle', 0.5, 0.5)), 'aiguille : pas de repère de défaut');
    // remplissage depuis le centre : l'arc de valeur ne part pas de la butée (135°)
    const fromStart = BC.knobSvg('index', 0.75, null, undefined, null).match(/ctl-knob-fill[^>]*d="M([\d.]+) ([\d.]+)/);
    const fromMid = BC.knobSvg('index', 0.75, null, undefined, 0.5).match(/ctl-knob-fill[^>]*d="M([\d.]+) ([\d.]+)/);
    ok(fromStart && fromMid && fromStart[1] !== fromMid[1], 'index : remplissage depuis le zéro quand il est donné');
}

// ─── Feuille de style : un commentaire fermé trop tôt efface la règle suivante ─────────────
// Vécu (2026-10-13) : « --bg-*/ » dans l'en-tête fermait le commentaire ; le navigateur jetait le
// bloc :root qui suivait → toutes les couleurs dérivées vides, pistes et vumètres invisibles.
const css = require('fs').readFileSync(path.join(__dirname, '..', 'static', 'css', 'controls.css'), 'utf8');
const sansComm = css.replace(/\/\*[\s\S]*?\*\//g, '');
ok(!sansComm.includes('*/'), 'CSS : aucun « */ » hors commentaire (commentaire fermé trop tôt)');
{
    const m = /:root\s*\{[^}]*--ctl-track:/.exec(sansComm);
    const avant = m ? sansComm.slice(0, m.index).trim() : null;
    ok(m && (avant === '' || avant.endsWith('}')), 'CSS : le bloc :root des jetons du moteur est une règle intacte (la précédente est fermée)');
}
const jetons = new Set([...sansComm.matchAll(/(--ctl-[a-z0-9-]+)\s*:/g)].map((m) => m[1]));
const utilises = [...new Set([...sansComm.matchAll(/var\((--ctl-[a-z0-9-]+)\s*\)/g)].map((m) => m[1]))];
const js = require('fs').readFileSync(path.join(__dirname, '..', 'static', 'js', 'bobi-controls.js'), 'utf8');
for (const m of js.matchAll(/setProperty\('(--ctl-[a-z0-9-]+)'/g)) jetons.add(m[1]);   // posés en ligne par le JS
const orphelins = utilises.filter((v) => !jetons.has(v));
ok(orphelins.length === 0, 'CSS : tout var(--ctl-…) sans repli est défini (' + (orphelins.join(', ') || 'aucun orphelin') + ')');
const tk = [10, 5, 0, -10, -20, -30, -50, -Infinity].map((t) => db.toPos(t));
let gapMin = 1; for (let i = 1; i < tk.length; i++) gapMin = Math.min(gapMin, tk[i - 1] - tk[i]);
ok(gapMin * 136 >= 13, 'repères par défaut du fader dB espacés d\'au moins 13 px sur une course de 136 px (' + (gapMin * 136).toFixed(1) + ')');
{   // jeu de repères du contexte serré (tranches de console) : sur la course minimale de 160 px
    const t2 = [10, 0, -10, -30, -Infinity].map((t) => db.toPos(t));
    let g2 = 1; for (let i = 1; i < t2.length; i++) g2 = Math.min(g2, t2[i - 1] - t2[i]);
    ok(g2 * 160 >= 20, 'repères du contexte serré (+10, 0, −10, −30, −∞) espacés d\'au moins 20 px sur 160 px (' + (g2 * 160).toFixed(1) + ')');
}

console.log(echecs ? '\n✗ ' + echecs + ' échec(s)' : '\n✓ tout passe');
process.exit(echecs ? 1 : 0);
