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

// ─── T-barre : butée, avancement, fin de course, clavier (promue du mélangeur le 2026-10-26) ───
{
    const M = BC.tbarModel();
    ok(M.rest === 0 && M.progress() === 0, 'T-barre : au repos en haut, avancement 0');
    M.setProgress(0.3);
    ok(near(M.pos, 0.3) && near(M.progress(), 0.3), 'T-barre butée haute : poignée à 30 % = avancement 0,3');
    M.rest = 1; M.setProgress(0.3);
    ok(near(M.pos, 0.7) && near(M.progress(), 0.3), 'T-barre butée basse : avancement 0,3 = poignée à 70 % (vers le haut)');
    M.setProgress(0.91); ok(!M.reached(), 'T-barre : 91 % ne termine pas');
    M.setProgress(0.92); ok(M.reached(), 'T-barre : 92 % termine');
    ok(M.finish() === 1 && M.pos === 0, 'T-barre butée basse : fin de course = poignée en haut');
    M.rest = 0; M.setProgress(0.95); M.finish();
    ok(M.pos === 1 && M.progress() === 1, 'T-barre butée haute : fin de course = poignée en bas');
    M.setPos(0);
    ok(near(M.key('ArrowDown', false), 0.05) && near(M.key('ArrowDown', true), 0.25), 'T-barre clavier : ↓ 5 %, Maj+↓ 20 %');
    ok(near(M.key('ArrowUp', false), 0.20) && M.key('End') === 1 && M.key('Home') === 0, 'T-barre clavier : ↑, Fin, Début');
    ok(M.key('a') === null && M.pos === 0, 'T-barre clavier : touche inconnue ignorée');
    M.rest = 1; M.setPos(0.5); M.key('ArrowDown');
    ok(near(M.progress(), 0.45), 'T-barre butée basse : ↓ ramène vers la butée (avancement 0,45)');
    ok(near(M.setProgress(1.7), 0) && near(M.setPos(-1), 0), 'T-barre : bornée à la course');
    const m5 = BC.tbarModel({ snap: 0.5 }); m5.setProgress(0.5);
    ok(m5.reached(), 'T-barre : seuil de fin réglable');
}

// ─── Courbes à poignées (2026-10-29) ───────────────────────────────────────────────────
{
    const C = BC.curvesModel([{ key: 'off', axis: 'x', x: 0.3, y: 0, xmax: 0.9 }, { key: 'dep', axis: 'y', x: 0.65, y: 0.5, ymin: 0.05 }]);
    const r = C.move('off', 0.2, 0.5);
    ok(near(r.x, 0.5) && r.y === 0, 'courbes : poignée sur l\'axe x — y ne bouge pas');
    ok(near(C.move('off', 1, 0).x, 0.9), 'courbes : borne xmax respectée');
    ok(near(C.move('dep', 0.3, -2).y, 0.05) && near(C.get('dep').x, 0.65), 'courbes : axe y, borne ymin, x inchangé');
    ok(near(C.key('dep', 'ArrowUp', false).y, 0.06) && near(C.key('dep', 'ArrowUp', true).y, 0.16), 'courbes clavier : ↑ 1 %, Maj 10 %');
    ok(near(C.key('off', 'ArrowLeft', false).x, 0.89) && near(C.key('off', 'ArrowDown', false).x, 0.88), 'courbes clavier : axe x par ← et ↓');
    ok(C.key('off', 'Home').x === 0 && near(C.key('off', 'End').x, 0.9), 'courbes clavier : Début / Fin = bornes');
    ok(C.key('off', 'a') === null && C.move('nope', 1, 1) === null, 'courbes : touche ou poignée inconnue ignorée');
    C.limits('dep', { xmin: 0.7 });
    ok(near(C.get('dep').x, 0.7), 'courbes : nouvelles bornes appliquées à la position');
    ok(near(C.place('dep', 0.95, 2).y, 1) && near(C.get('dep').x, 0.95), 'courbes : placement borné');
}

// ─── Rotatif : molette (2026-10-31) — faux DOM minimal, VRAI moteur ────────────────────
// Un cran = un pas, Maj = pas fin, la page ne défile pas sur le rotatif (preventDefault), un
// événement sans mouvement ne touche à rien, Maj+molette en deltaX (Chrome/Linux/Windows) compte.
// Le mélangeur a retiré sa molette privée (0.23.65) : celle-ci est la seule.
// Mutations du 31/10 (toutes rouges) : écouteur retiré, deltaY seul, delta nul = un cran vers le bas
// (le défaut d'avant), Maj ignorée, sans preventDefault, désactivé ignoré, sans émission « change ».
{
    const vm = require('vm'), fs = require('fs');
    function node(tag) {
        const L = {}, attrs = {}, cls = new Set(), kids = [];
        const n = {
            tagName: String(tag || 'div').toUpperCase(), style: { setProperty() {} }, dataset: {}, innerHTML: '', textContent: '',
            className: '', children: kids,
            classList: { add: (...c) => c.forEach((x) => cls.add(x)), remove: (...c) => c.forEach((x) => cls.delete(x)),
                         toggle: (c, on) => { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); }, contains: (c) => cls.has(c) },
            setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: (k) => (k in attrs ? attrs[k] : null),
            hasAttribute: (k) => k in attrs, removeAttribute: (k) => { delete attrs[k]; },
            addEventListener: (t, f) => { (L[t] = L[t] || []).push(f); }, removeEventListener: (t, f) => { L[t] = (L[t] || []).filter((x) => x !== f); },
            dispatchEvent: (e) => { (L[e.type] || []).forEach((f) => f(e)); return true; },
            appendChild: (c) => { kids.push(c); return c; },
            querySelector: (s) => { const c = s.replace(/^\./, ''); return kids.find((k) => k.className.split(' ').includes(c)) || null; },
            setPointerCapture() {}, focus() {},
            fire(type, props) { const e = Object.assign({ type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, props);
                                (L[type] || []).forEach((f) => f(e)); return e; },
        };
        return n;
    }
    const win = { document: { createElement: node, createElementNS: (_, t) => node(t) },
                  getComputedStyle: () => ({ getPropertyValue: () => 'arc' }),
                  requestAnimationFrame: () => 0, cancelAnimationFrame() {},
                  CustomEvent: function (type) { this.type = type; } };
    const sb = { window: win, console };
    vm.createContext(sb);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'bobi-controls.js'), 'utf8'), sb);
    const D = win.BobiControls;
    const host = node('div');
    const k = D.create('knob', host, { min: 0, max: 1, step: 0.01, default: 0.5, value: 0.5 });
    const hit = host.querySelector('.ctl-knob-hit');
    const changes = [];
    k.on('change', (v) => changes.push(v));
    let e = hit.fire('wheel', { deltaY: -100, deltaX: 0, shiftKey: false });
    ok(near(k.get(), 0.51) && e.defaultPrevented, 'molette vers le haut : +1 pas, la page ne défile pas');
    hit.fire('wheel', { deltaY: 100, deltaX: 0, shiftKey: false });
    hit.fire('wheel', { deltaY: 100, deltaX: 0, shiftKey: false });
    ok(near(k.get(), 0.49), 'molette vers le bas : −1 pas par cran');
    hit.fire('wheel', { deltaY: -100, deltaX: 0, shiftKey: true });
    ok(near(k.get(), 0.491), 'Maj + molette : pas fin (pas / 10)');
    hit.fire('wheel', { deltaY: 0, deltaX: -100, shiftKey: true });
    ok(near(k.get(), 0.492), 'Maj + molette livrée en deltaX (Chrome) : compte, et dans le bon sens');
    const before = k.get();
    e = hit.fire('wheel', { deltaY: 0, deltaX: 0, shiftKey: false });
    ok(k.get() === before && !e.defaultPrevented, 'événement sans mouvement : valeur intacte, défilement laissé à la page');
    ok(changes.length === 5 && near(changes[4], 0.492), 'chaque cran émet « change » (valeur envoyée) : ' + changes.length);
    k.setDisabled(true);
    e = hit.fire('wheel', { deltaY: -100, deltaX: 0 });
    ok(k.get() === before && !e.defaultPrevented, 'désactivé : la molette ne change rien et ne bloque pas la page');
    k.setDisabled(false);
    for (let i = 0; i < 80; i++) hit.fire('wheel', { deltaY: -100, deltaX: 0 });
    ok(k.get() === 1, 'molette bornée au max');
}

console.log(echecs ? '\n✗ ' + echecs + ' échec(s)' : '\n✓ tout passe');
process.exit(echecs ? 1 : 0);
