// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Éditeur de modèles de PiP — « Rogner l'espace inutilisé » rogne sur les QUATRE côtés, en tenant
// compte de l'ANCRAGE des composants.
//
// ★ LE CAS (2026-10-29, modèle « Atelier » sur la Z8) : format 1,939, vidéo 16:9 calée à gauche, UMD
// dessous à 100 % de large — mais 100 % de l'IMAGE (ancrage horizontal par défaut d'un UMD). Le
// rognage raisonnait en fractions de CELLULE, croyait l'UMD pleine largeur, et laissait la bande
// noire à droite. On exécute ici les VRAIES fonctions de static/pip_editor.js.
//
//   $ node tests/verif_pip_rogner.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'pip_editor.js'), 'utf8');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
function fn(nom) {
    const i = src.indexOf('\nfunction ' + nom + '(');
    if (i < 0) { console.log('  ÉCHEC introuvable : ' + nom); process.exit(1); }
    const re = /\n\}\n/g; re.lastIndex = i + 1;
    return src.slice(i, re.exec(src).index + 3);
}
function ligne(debut) {
    const i = src.indexOf(debut);
    if (i < 0) { console.log('  ÉCHEC introuvable : ' + debut); process.exit(1); }
    return src.slice(i, src.indexOf('\n', i));
}
const code = [
    'let cur = null, cellW = 640;',
    'const comps = () => (cur && cur.config && cur.config.components) || [];',
    ligne('const _videoK = '), ligne('const _ANCHOR_X_IMAGE_TYPES = '),
    ...['tplAspect', 'fmtAspect', '_enforceVideoRatio', 'compAnchor', 'pxRect', 'framePxRect', 'imgPxRect',
        'trimTemplate', 'applySimCell'].map(fn),
    'function _resizeCanvasDisplay() {} function refreshProps() {} function draw() {} function syncAspectField() {}',
    'function flash(m, k) { flashes.push([m, k]); } function _t(k, d) { return d; }',
    'this.setCur = c => { cur = c; }; this.getCur = () => cur;',
].join('\n');
const canvas = {width: 640, height: 330};
const ctx = {Math, String, parseInt, parseFloat, flashes: [], cvs: () => canvas, $: () => canvas};
vm.createContext(ctx);
vm.runInContext(code, ctx);

function cas(aspect, compsList) {
    ctx.setCur({config: {aspect, components: compsList}});
    vm.runInContext('applySimCell()', ctx);
}
const px = c => vm.runInContext('pxRect', ctx)(c);

// ═══ Le cas signalé : vidéo 16:9 à gauche, UMD « 100 % de l'image » dessous ═══
// Le cas signalé était en « remplir » (le défaut d'alors) : on le fige tel quel.
const video = {id: 'video', type: 'video', x: 0, y: 0, w: 0.837, h: 0.913, border: 'viewfinder', border_w: 3, fit: 'fill'};
const umd = {id: 'umd', type: 'umd', x: 0, y: 0.913, w: 1, h: 0.087};
cas(1.939, [video, umd]);
const avant = px(umd);
controle('situation de départ : l\'UMD n\'occupe QUE la largeur de l\'image (pas la cellule)',
         avant.x + avant.w < canvas.width - 50, JSON.stringify(avant) + ' / toile ' + canvas.width);
vm.runInContext('trimTemplate()', ctx);
const a = ctx.getCur().config.aspect;
controle('★★★ le format rétrécit à droite (1,939 → largeur de l\'image)', a < 1.7 && a > 1.55, 'aspect ' + a);
const rv = px(video), ru = px(umd);
controle('★★★ après rognage, la vidéo va jusqu\'au bord droit', Math.abs(rv.x + rv.w - canvas.width) <= 3,
         JSON.stringify(rv) + ' / toile ' + canvas.width);
controle('★★ l\'UMD aussi', Math.abs(ru.x + ru.w - canvas.width) <= 3, JSON.stringify(ru));
controle('★★ et jusqu\'en bas', Math.abs(ru.y + ru.h - canvas.height) <= 3, JSON.stringify(ru) + ' / ' + canvas.height);
controle('★ la vidéo reste 16:9 en pixels', Math.abs(rv.w / rv.h - 16 / 9) < 0.02, (rv.w / rv.h).toFixed(3));
controle('★ l\'ancrage sur l\'image est conservé (fractions inchangées)', umd.x === 0 && umd.w === 1);

// ═══ Même cas en « contenir » (défaut depuis le 29/10) : l'image garde son 16:9 centrée dans son
// cadre, la réserve des équerres laisse une petite marge — l'UMD, calé sur l'image, s'y arrête. ═══
const videoC = Object.assign({}, video, {w: 0.837, h: 0.913, fit: 'contain'});
const umdC = {id: 'umd', type: 'umd', x: 0, y: 0.913, w: 1, h: 0.087};
cas(1.939, [videoC, umdC]);
vm.runInContext('trimTemplate()', ctx);
const rc = px(umdC);
controle('★★ en « contenir », le rognage à droite tient aussi (marge de réserve ≤ 8 px)',
         ctx.getCur().config.aspect < 1.7 && canvas.width - (rc.x + rc.w) <= 8, JSON.stringify(rc) + ' / ' + canvas.width);

// ═══ Rognage par le bas (ce qui marchait) : toujours bon ═══
const v2 = {id: 'video', type: 'video', x: 0, y: 0, w: 1, h: 0.8};
const t2 = {id: 'umd', type: 'umd', x: 0, y: 0.8, w: 1, h: 0.08, anchor_x: 'cell'};
cas(16 / 9 * 0.8, [v2, t2]);   // cellule plus haute que vidéo + bandeau : du vide en bas
vm.runInContext('trimTemplate()', ctx);
const r2 = px(t2);
controle('★ espace vide EN BAS : rogné', Math.abs(r2.y + r2.h - canvas.height) <= 3, JSON.stringify(r2) + ' / ' + canvas.height);

// ═══ Rien à rogner ═══
ctx.flashes.length = 0;
cas(16 / 9, [{id: 'video', type: 'video', x: 0, y: 0, w: 1, h: 1}]);
vm.runInContext('trimTemplate()', ctx);
controle('cellule pleine : « rien à rogner », format inchangé',
         ctx.getCur().config.aspect === 16 / 9 && ctx.flashes.some(f => f[1] === 'warn'));

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
